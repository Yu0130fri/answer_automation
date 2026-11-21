import csv
import json
import logging
import os
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.select import Select
from typing import Optional, List
from urllib.parse import urlparse, urljoin

try:
    import psutil
except Exception:
    psutil = None

_CURRENT_DIR = Path(__file__).absolute().parents[2]
_DRIVER_PATH = _CURRENT_DIR / "driver/chromedriver"
cookies_file = _CURRENT_DIR / "moppy.pkl"  # クッキーを保存するファイルの名前
_UNABLE_TO_URL = _CURRENT_DIR / "unable_to_answer.csv"
_ANSWERED_URLS = _CURRENT_DIR / "answered.csv"  # 既に回答したURLを記録するファイル
_PARALLEL_STATE_FILE = _CURRENT_DIR / ".moppy_parallel_state.json"

login_url = "https://ssl.pc.moppy.jp/login/"
questionnaire_url = "https://pc.moppy.jp/research/"

options = Options()
options.add_argument("--headless")

# ページに排他や混雑を示す文言がある場合に検出するためのキーワード
LOCK_KEYWORDS = [
    "排他",
    "ただいま混み合",
    "混雑",
    "アクセスが集中",
    "回答できません",
    "アクセス制限",
]


class AnswerQuestionnaire:
    def __init__(self, email: str, password: str) -> None:
        self._cookie_file = cookies_file
        self._questionnaire_url = questionnaire_url
        self._options = Options()
        self._login_url = login_url
        self._email = email
        self._password = password
        self._state_lock = threading.Lock()
        self._in_progress_urls: List[str] = []

        # load unable-to-answer list
        try:
            with open(_UNABLE_TO_URL, "r") as f:
                reader = csv.reader(f)
                unable_to_answer_urls = [url[0] for url in reader]
        except FileNotFoundError:
            unable_to_answer_urls = []

        # load already answered list if exists
        answered_urls = []
        if os.path.exists(_ANSWERED_URLS):
            try:
                with open(_ANSWERED_URLS, "r") as f2:
                    reader2 = csv.reader(f2)
                    answered_urls = [url[0] for url in reader2]
            except Exception:
                answered_urls = []

        self._unable_to_answer_urls = unable_to_answer_urls
        self._answered_urls = answered_urls
        self._load_resume_state()
        self._unable_to_answer_urls = list(dict.fromkeys(self._unable_to_answer_urls))
        self._answered_urls = list(dict.fromkeys(self._answered_urls))
        self._in_progress_urls = list(dict.fromkeys(self._in_progress_urls))

    def _option_add_argument(self) -> None:
        self._options.add_argument("--headless")

    def _create_driver(self) -> webdriver.Chrome:
        self._option_add_argument()
        # 軽量化オプション: 画像や自動再生を無効にしてレンダリング負荷を下げる
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.media_stream": 2,
        }
        try:
            self._options.add_experimental_option("prefs", prefs)
        except Exception:
            # 古いオプションAPIで失敗しても続行
            pass
        # 一般的な軽量化フラグ
        self._options.add_argument("--disable-gpu")
        self._options.add_argument("--disable-extensions")
        self._options.add_argument("--disable-dev-shm-usage")
        self._options.add_argument("--no-sandbox")
        # 自動再生を抑制
        self._options.add_argument("--autoplay-policy=user-gesture-required")

        if _DRIVER_PATH.exists():
            return webdriver.Chrome(
                service=Service(str(_DRIVER_PATH)), options=self._options
            )
        return webdriver.Chrome(options=self._options)

    def _load_cookies(self, driver: webdriver.Chrome) -> None:
        if not os.path.exists(self._cookie_file):
            return
        cookies = pickle.load(open(self._cookie_file, "rb"))
        driver.get(self._login_url)
        for c in cookies:
            try:
                driver.add_cookie(c)
            except Exception:
                continue

    def _detect_lock(self, driver: webdriver.Chrome) -> bool:
        try:
            text = driver.page_source
            for kw in LOCK_KEYWORDS:
                if kw in text:
                    return True
        except Exception:
            return False
        return False

    def _is_already_answered_page(self, driver: webdriver.Chrome) -> bool:
        """終了・回答済みメッセージが表示されているかを判定する。"""
        def has_completion_message(text: str) -> bool:
            normalized_text = "".join(text.split())
            if "該当のアンケートは終了、またはご回答済みです" in normalized_text:
                return True
            return (
                "アンケート" in normalized_text
                and "終了" in normalized_text
                and "ご協力いただきありがとうございます" in normalized_text
            )

        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
            if has_completion_message(page_text):
                return True
        except Exception:
            pass

        try:
            return has_completion_message(driver.page_source)
        except Exception:
            return False

    def _click_survey_start_button(self, driver: webdriver.Chrome) -> bool:
        """文言からアンケート開始ボタンを探してクリックする。"""
        buttons = driver.find_elements(
            By.XPATH,
            "//button[contains(normalize-space(.), 'アンケートを開始')]",
        )
        for button in buttons:
            try:
                if not button.is_displayed() or not button.is_enabled():
                    continue
                try:
                    button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", button)
                return True
            except Exception:
                continue
        return False

    def _has_email_question(self, driver: webdriver.Chrome) -> bool:
        """ページ内にメールアドレス入力欄があるかをチェックする"""
        try:
            elems = driver.find_elements(
                By.XPATH,
                "//input[@type='email']|"
                + "//input[contains(translate(@name,'MAIL','mail'),'mail')]|"
                + "//input[contains(translate(@placeholder,'メール','メール'),'メール')]",
            )
            return len(elems) > 0
        except Exception:
            return False

    def _is_page_answerable(self, driver: webdriver.Chrome) -> bool:
        """ページが実際に回答可能かを判定する。
        以下の条件を確認：
        1. 回答済みの表示がないか
        2. フォーム要素（radio, checkbox, select, textarea）が存在するか
        """
        try:
            if self._is_already_answered_page(driver):
                return False

            page_source = driver.page_source

            # 回答済みを示す一般的なテキストをチェック
            answered_keywords = [
                "回答済み",
                "ご回答済み",
                "終了しました",
                "受け付けを終了",
            ]
            for keyword in answered_keywords:
                if keyword in page_source:
                    return False

            # フォーム要素が存在するかチェック
            form_elements = driver.find_elements(
                By.XPATH,
                "//input[@type='radio']|"
                + "//input[@type='checkbox']|"
                + "//select|"
                + "//textarea",
            )
            return len(form_elements) > 0
        except Exception:
            # エラーが出たら、とりあえず回答可能と判定する
            return True

    def _check_driver_resource(
        self,
        driver: webdriver.Chrome,
        cpu_thresh: float = 80.0,
        mem_mb_thresh: float = 500.0,
    ):
        """ドライバ（とその子プロセス）のCPU%とRSS(MB)を取得し、閾値を超えているか判定する。
        psutil がなければ常に False を返す。
        戻り値: (overloaded: bool, cpu_percent: float, mem_mb: float)
        """
        if psutil is None:
            return False, 0.0, 0.0

        try:
            pid = None
            try:
                pid = driver.service.process.pid
            except Exception:
                # Selenium の別実装向け
                try:
                    pid = driver.service.process._proc.pid
                except Exception:
                    pid = None

            if pid is None:
                return False, 0.0, 0.0

            p = psutil.Process(pid)
            children = p.children(recursive=True)

            # cpu_percent は 2 回呼び出す必要がある実装もあるため、短時間のサンプルを取る
            total_cpu = p.cpu_percent(interval=0.1)
            total_rss = p.memory_info().rss
            for c in children:
                try:
                    total_cpu += c.cpu_percent(interval=0.0)
                    total_rss += c.memory_info().rss
                except Exception:
                    continue

            mem_mb = total_rss / (1024 * 1024)
            overloaded = (total_cpu > cpu_thresh) or (mem_mb > mem_mb_thresh)
            return overloaded, total_cpu, mem_mb
        except Exception:
            return False, 0.0, 0.0

    def _restart_driver_preserve_cookies(self, driver: webdriver.Chrome):
        """現在のdriverからクッキーを取得して安全に再起動し、可能なら元のURLへ復帰する。"""
        cookies = []
        current = None
        try:
            cookies = driver.get_cookies() or []
        except Exception:
            cookies = []
        try:
            current = driver.current_url
        except Exception:
            current = None

        try:
            driver.quit()
        except Exception:
            try:
                driver.close()
            except Exception:
                pass

        new_driver = self._create_driver()

        # cookies を追加するためにベースドメインへ移動
        if cookies:
            try:
                base = (
                    urlparse(self._login_url).scheme
                    + "://"
                    + urlparse(self._login_url).hostname
                )
                new_driver.get(base)
                for c in cookies:
                    try:
                        # Selenium requires domain/path consistency; add only safe keys
                        new_driver.add_cookie(c)
                    except Exception:
                        continue
            except Exception:
                pass

        # 元のページへ戻す
        if current:
            try:
                new_driver.get(current)
            except Exception:
                pass

        return new_driver

    def save_cookie_as_pickle(self) -> None:
        """一度ログインしてcookieを保存、その後cookieを保持してアンケート画面へ遷移する"""
        if os.path.exists(self._cookie_file):
            os.remove(self._cookie_file)

        self._option_add_argument()
        driver = self._create_driver()
        driver.get(self._login_url)

        # login
        email_form = driver.find_element(By.XPATH, "//input[@name='mail']")
        email_form.send_keys(self._email)
        password_form = driver.find_element(By.XPATH, "//input[@name='pass']")
        password_form.send_keys(self._password)
        sleep(3)

        submit_button = driver.find_element(
            By.XPATH, "//button[@data-ga-label='ログイン']"
        )
        submit_button.submit()

        if self._check_success_login(driver):
            logging.info("login success!")
        else:
            logging.info("login failed!")

        cookies = driver.get_cookies()  # クッキーを取得する
        pickle.dump(cookies, open(cookies_file, "wb"))  # クッキーを保存する
        driver.quit()  # ウィンドウを閉じる

    def _check_success_login(self, driver: webdriver.Chrome) -> bool:
        """ログインした状態かどうかを検証する"""
        is_login = driver.find_element(
            By.XPATH, "//a[@data-ga-label='ログアウト']"
        ).text
        if is_login == "ログアウト":
            return True

        return False

    def _login_to_site(self, driver: webdriver.Chrome) -> None:
        """ログイン画面へ遷移して認証を実施する。"""
        driver.get(self._login_url)
        email_form = driver.find_element(By.XPATH, "//input[@name='mail']")
        email_form.clear()
        email_form.send_keys(self._email)
        password_form = driver.find_element(By.XPATH, "//input[@name='pass']")
        password_form.clear()
        password_form.send_keys(self._password)
        sleep(2)

        submit_button = driver.find_element(
            By.XPATH, "//button[@data-ga-label='ログイン']"
        )
        submit_button.submit()

        if not self._check_success_login(driver):
            raise RuntimeError("Moppy login failed")

    def _add_url_to_list(self, url: str, *, collection: str) -> None:
        if not url:
            return
        target = getattr(self, f"_{collection}")
        with self._state_lock:
            if url not in target:
                target.append(url)
            if collection == "answered_urls":
                _write_answered_urls(self._answered_urls)
        self._save_resume_state()

    def _worker_log(self, worker_id: int, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [worker-{worker_id}] {message}", flush=True)

    def _load_resume_state(self) -> None:
        if not os.path.exists(_PARALLEL_STATE_FILE):
            return
        try:
            with open(_PARALLEL_STATE_FILE, "r") as f:
                payload = json.load(f)
            if isinstance(payload.get("answered_urls"), list):
                self._answered_urls = payload["answered_urls"]
            if isinstance(payload.get("unable_to_answer_urls"), list):
                self._unable_to_answer_urls = payload["unable_to_answer_urls"]
            if isinstance(payload.get("in_progress_urls"), list):
                self._in_progress_urls = payload["in_progress_urls"]
        except Exception:
            self._in_progress_urls = []

    def _save_resume_state(self) -> None:
        payload = {
            "answered_urls": self._answered_urls,
            "unable_to_answer_urls": self._unable_to_answer_urls,
            "in_progress_urls": self._in_progress_urls,
        }
        with open(_PARALLEL_STATE_FILE, "w") as f:
            json.dump(payload, f)

    def _mark_in_progress(self, url: str, worker_id: int) -> None:
        if not url:
            return
        with self._state_lock:
            if url not in self._in_progress_urls:
                self._in_progress_urls.append(url)
        self._save_resume_state()
        self._worker_log(worker_id, f"開始: {url}")

    def _mark_done(self, url: str, worker_id: int, *, success: bool) -> None:
        if not url:
            return
        with self._state_lock:
            self._in_progress_urls = [u for u in self._in_progress_urls if u != url]
            if success and url not in self._answered_urls:
                self._answered_urls.append(url)
            elif not success and url not in self._unable_to_answer_urls:
                self._unable_to_answer_urls.append(url)
        self._save_resume_state()
        self._worker_log(
            worker_id,
            f"完了: {url} ({'answered' if success else 'skipped'})",
        )

    def _write_shared_csv(self) -> None:
        with self._state_lock:
            _write_unable_to_answer_urls(self._unable_to_answer_urls)
            _write_answered_urls(self._answered_urls)
            if os.path.exists(_PARALLEL_STATE_FILE):
                os.remove(_PARALLEL_STATE_FILE)

    def get_questionnaire_urls(
        self, driver: Optional[webdriver.Chrome] = None
    ) -> List[str]:
        """アンケートURLを全て取得する（DBやjsonなどに保存するかは検討中）
        引数に`driver`を渡すとそのドライバを使って取得する。
        """
        created_driver = False
        if driver is None:
            self._option_add_argument()
            driver = webdriver.Chrome(
                executable_path=_DRIVER_PATH, options=self._options
            )
            created_driver = True

            cookies = pickle.load(open(cookies_file, "rb"))
            driver.get(login_url)

            # login and add cookie
            for c in cookies:
                driver.add_cookie(c)
            driver.get(questionnaire_url)
            sleep(2)
        else:
            # assume driver is already logged in or has cookies set
            driver.get(questionnaire_url)
            sleep(2)

        count_eligible_questionnaire_str = (
            str(driver.find_element(By.XPATH, "//span[@class = 'a-list__total']").text)
            .strip()
            .replace("(", "")
            .replace("件)", "")
        )
        count_eligible_questionnaire: int
        try:
            count_eligible_questionnaire = int(count_eligible_questionnaire_str)
        except ValueError:
            count_eligible_questionnaire = 30

        count_scroll = count_eligible_questionnaire // 5
        for _ in range(count_scroll):
            try:
                scroll_btn = driver.find_element(By.CLASS_NAME, "a-btn__more--down")
                scroll_btn.click()
                sleep(2)
            except Exception:
                break

        able_to_answer_urls = driver.find_elements(
            By.XPATH, "//a[contains(@href,'r_id')]"
        )

        if len(able_to_answer_urls) == 0:
            if created_driver:
                driver.close()
            raise ValueError("回答できるアンケートが存在しませんでした。")

        urls = [url.get_attribute("href") for url in able_to_answer_urls]
        # skip URLs we've already answered
        urls = [u for u in urls if u not in self._answered_urls]

        # ドライバを自分たちで作成した場合のみ、URLを検証して不要なものをフィルタリング
        # (後から使う場合は、ドライバの状態を変えずに返すため、フィルタリングは行わない)
        if created_driver:
            validated_urls = []
            for url in urls:
                try:
                    driver.get(url)
                    sleep(1)
                    if self._is_already_answered_page(driver):
                        print(f"既に回答済みまたは終了済みです: {url}")
                        self._add_url_to_list(url, collection="answered_urls")
                    elif self._is_page_answerable(driver):
                        validated_urls.append(url)
                    else:
                        print(f"既に回答済みまたは回答不可のため除外: {url}")
                except Exception as e:
                    print(f"URL検証エラー: {url} - {str(e)}")
                    # エラーが出たものは validated_urls に含めない（保守的判定）

            driver.close()
            return validated_urls
        else:
            # ドライバが渡されている場合は、フィルタリングなしで返す
            # (呼び出し元で続けて使う可能性があるため)
            return urls

    def check_policy_checkbox(self, driver: webdriver.Chrome) -> None:
        try:
            checkboxes = driver.find_elements(
                By.XPATH, "//input[@type='checkbox']"
            )
            policy_terms = ("同意", "確認", "規約", "承諾")
            policy_attributes = ("agree", "consent", "policy", "terms")
            for checkbox in checkboxes:
                try:
                    qnr_lists = checkbox.find_elements(
                        By.XPATH,
                        "./ancestor::ul[contains(concat(' ', normalize-space(@class), ' '), "
                        "' qnr-q-ch-list ')]",
                    )
                    if qnr_lists:
                        continue

                    checkbox_id = checkbox.get_attribute("id") or ""
                    checkbox_name = checkbox.get_attribute("name") or ""
                    labels = (
                        driver.find_elements(
                            By.XPATH, f"//label[@for='{checkbox_id}']"
                        )
                        if checkbox_id
                        else []
                    )
                    labels.extend(
                        checkbox.find_elements(
                            By.XPATH, "./ancestor::label[1]"
                        )
                    )
                    label_text = " ".join(
                        " ".join(label.text.split()) for label in labels
                    )
                    attributes = f"{checkbox_id} {checkbox_name}".lower()
                    is_policy_checkbox = any(
                        term in label_text for term in policy_terms
                    ) or any(
                        term in attributes for term in policy_attributes
                    )
                    if is_policy_checkbox:
                        self._click_checkbox_input(driver, checkbox)
                except Exception:
                    continue

        except Exception:
            return

    def _click_radio_elem(self, driver: webdriver.Chrome, elem) -> bool:
        """ラジオ本体と関連するラベル・行を試し、選択状態を確認する。"""
        if self._wait_for_radio_selection(elem, attempts=1):
            return True

        targets = [elem]
        try:
            elem_id = elem.get_attribute("id")
            if elem_id:
                targets.extend(
                    driver.find_elements(By.XPATH, f"//label[@for='{elem_id}']")
                )
        except Exception:
            pass
        for xpath in (
            "./following-sibling::label[1]",
            "./preceding-sibling::label[1]",
            "./ancestor::label[1]",
            "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-form ')][1]",
            "./ancestor::li[1]",
            "./ancestor::td[1]",
            "./ancestor::tr[1]",
        ):
            try:
                targets.append(elem.find_element(By.XPATH, xpath))
            except Exception:
                continue

        for target in targets:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target
                )
            except Exception:
                pass
            try:
                target.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", target)
                except Exception:
                    continue

            if self._wait_for_radio_selection(elem):
                return True

        # Some survey widgets hide the input and bind their handler to a wrapper.
        # Try the native radio click after the associated label targets.
        try:
            driver.execute_script("arguments[0].click();", elem)
        except Exception:
            return False
        return self._wait_for_radio_selection(elem)

    def _wait_for_radio_selection(self, radio, attempts: int = 5) -> bool:
        for _ in range(attempts):
            try:
                if radio.is_selected():
                    return True
            except Exception:
                return False
            sleep(0.1)
        return False

    def click_radio(self, driver: webdriver.Chrome) -> None:
        # デフォルトではvalue='1'を優先する
        try:
            if self._click_msa_radio_matrix(driver):
                return

            if self._click_matrix_radio_questions(driver):
                return

            if self._click_aria_radio_groups(driver):
                return

            if self._click_qnr_radio_groups(driver):
                return

            if self._click_native_radio_groups(driver):
                return

            radio_buttons = driver.find_elements(
                By.XPATH, "//input[@type='radio'][@value='1']"
            )
            if len(radio_buttons) == 0:
                radios = driver.find_elements(By.XPATH, "//input[@type='radio']")
                for r in radios:
                    self._click_radio_elem(driver, r)
                return

            for radio in radio_buttons:
                self._click_radio_elem(driver, radio)
        except Exception:
            pass

    def click_radio_with_values(
        self, driver: webdriver.Chrome, values: List[str]
    ) -> None:
        """ラジオボタンをグループごとに優先値で選択する
        values は優先的に選ぶ value 属性のリスト
        """
        try:
            if self._click_msa_radio_matrix(driver):
                return

            if self._click_matrix_radio_questions(
                driver, preferred_values=values
            ):
                return

            if self._click_aria_radio_groups(
                driver, preferred_values=values
            ):
                return

            if self._click_qnr_radio_groups(driver, preferred_values=values):
                return

            if self._click_native_radio_groups(
                driver, preferred_values=values
            ):
                return

            radios = driver.find_elements(By.XPATH, "//input[@type='radio']")
            if len(radios) == 0:
                return

            # グループ化（name属性ごと）
            names = []
            for r in radios:
                try:
                    name = r.get_attribute("name")
                except Exception:
                    name = None
                if name and name not in names:
                    names.append(name)

            for name in names:
                clicked = False
                for v in values:
                    try:
                        elem = driver.find_element(
                            By.XPATH,
                            f"//input[@type='radio' and @name='{name}' and @value='{v}']",
                        )
                        if self._click_radio_elem(driver, elem):
                            clicked = True
                            break
                    except NoSuchElementException:
                        continue

                if not clicked:
                    try:
                        first_elem = driver.find_element(
                            By.XPATH, f"//input[@type='radio' and @name='{name}']"
                        )
                        self._click_radio_elem(driver, first_elem)
                    except Exception:
                        continue
        except Exception:
            return

    def _click_msa_radio_matrix(self, driver: webdriver.Chrome) -> bool:
        """MSA形式の表を行ごとに回答し、1番目・2番目の尺度を選ぶ。"""
        tables = driver.find_elements(
            By.XPATH,
            "//table[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix_v ')]",
        )
        if not tables:
            return False

        for table in tables:
            rows = table.find_elements(
                By.XPATH, ".//tr[.//input[@type='radio']]"
            )
            for row in rows:
                try:
                    radios = row.find_elements(
                        By.XPATH, ".//input[@type='radio']"
                    )
                    if any(radio.is_selected() for radio in radios):
                        continue
                except Exception:
                    continue

                ordered_radios = []
                for value in ("1", "2"):
                    ordered_radios.extend(
                        radio
                        for radio in radios
                        if radio.get_attribute("value") == value
                        and radio not in ordered_radios
                    )

                for radio in ordered_radios:
                    try:
                        if not radio.is_enabled():
                            continue
                    except Exception:
                        continue

                    targets = []
                    for xpath in (
                        "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                        "' matrix_answercell ')][1]",
                        "./ancestor::td[1]",
                    ):
                        try:
                            target = radio.find_element(By.XPATH, xpath)
                            if target not in targets:
                                targets.append(target)
                        except Exception:
                            continue
                    targets.append(radio)

                    for target in targets:
                        try:
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block: 'center'});",
                                target,
                            )
                        except Exception:
                            pass
                        try:
                            target.click()
                        except Exception:
                            try:
                                driver.execute_script(
                                    "arguments[0].click();", target
                                )
                            except Exception:
                                continue

                        for _ in range(10):
                            try:
                                if radio.is_selected():
                                    break
                            except Exception:
                                break
                            sleep(0.1)
                        else:
                            continue
                        break
                    else:
                        continue
                    break
        return True

    def _click_native_radio_groups(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> bool:
        """通常のradio inputをnameごとにまとめ、先頭の有効な選択肢を選ぶ。"""
        radios = driver.find_elements(By.XPATH, "//input[@type='radio']")
        if not radios:
            return False

        groups = {}
        for index, radio in enumerate(radios):
            try:
                name = radio.get_attribute("name")
            except Exception:
                name = None
            key = ("name", name) if name else ("unnamed", index)
            groups.setdefault(key, []).append(radio)

        for group in groups.values():
            try:
                if any(radio.is_selected() for radio in group):
                    continue
            except Exception:
                pass

            ordered_radios = []
            for value in preferred_values or []:
                ordered_radios.extend(
                    radio
                    for radio in group
                    if radio.get_attribute("value") == value
                    and radio not in ordered_radios
                )
            ordered_radios.extend(
                radio for radio in group if radio not in ordered_radios
            )

            for radio in ordered_radios:
                try:
                    if radio.is_enabled() and self._click_radio_elem(driver, radio):
                        break
                except Exception:
                    continue
        return True

    def _click_aria_radio_groups(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> bool:
        """ARIA role=radio の選択肢をグループごとに選び、状態を確認する。"""
        radios = driver.find_elements(By.XPATH, "//*[@role='radio']")
        if not radios:
            return False

        groups = {}
        for index, radio in enumerate(radios):
            group_key = None
            for xpath in (
                "./ancestor::*[@role='radiogroup'][1]",
                "./ancestor::*[count(.//*[@role='radio']) > 1][1]",
            ):
                try:
                    group = radio.find_element(By.XPATH, xpath)
                    group_key = ("container", group.id)
                    break
                except Exception:
                    continue

            if group_key is None:
                try:
                    name = radio.get_attribute("name")
                except Exception:
                    name = None
                group_key = ("name", name) if name else ("individual", index)
            groups.setdefault(group_key, []).append(radio)

        for group in groups.values():
            try:
                if any(
                    radio.get_attribute("aria-checked") == "true"
                    or radio.get_attribute("data-state") == "checked"
                    for radio in group
                ):
                    continue
            except Exception:
                pass

            ordered_radios = []
            for value in preferred_values or []:
                ordered_radios.extend(
                    radio
                    for radio in group
                    if radio.get_attribute("value") == value
                    and radio not in ordered_radios
                )
            ordered_radios.extend(
                radio for radio in group if radio not in ordered_radios
            )

            for radio in ordered_radios:
                try:
                    if radio.get_attribute("aria-disabled") == "true":
                        continue
                    if radio.get_attribute("disabled") is not None:
                        continue
                    targets = [radio]
                    try:
                        targets.append(
                            radio.find_element(By.XPATH, "./ancestor::label[1]")
                        )
                    except Exception:
                        pass

                    for target in targets:
                        try:
                            target.click()
                        except Exception:
                            try:
                                driver.execute_script(
                                    "arguments[0].click();", target
                                )
                            except Exception:
                                continue
                        if (
                            radio.get_attribute("aria-checked") == "true"
                            or radio.get_attribute("data-state") == "checked"
                        ):
                            break
                    if (
                        radio.get_attribute("aria-checked") == "true"
                        or radio.get_attribute("data-state") == "checked"
                    ):
                        break
                except Exception:
                    continue
        return True

    def _click_matrix_radio_questions(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> bool:
        """折りたたみ式のマトリクス設問を順に開き、各設問の回答を選ぶ。"""
        matrix_item_xpath = (
            "//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix-item ')]"
        )
        matrix_items = driver.find_elements(By.XPATH, matrix_item_xpath)
        if not matrix_items:
            return False

        start_buttons = driver.find_elements(
            By.XPATH,
            "//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix-start ')]",
        )
        for button in start_buttons:
            try:
                if button.is_displayed():
                    try:
                        button.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", button)
                    sleep(1)
                    break
            except Exception:
                continue

        for index in range(len(matrix_items)):
            try:
                # 再取得して、前の回答後に画面が更新された場合の stale element を避ける。
                current_items = driver.find_elements(By.XPATH, matrix_item_xpath)
                if index >= len(current_items):
                    break
                item = current_items[index]
                section = item.find_element(
                    By.XPATH,
                    "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                    "' qnr-q-ch-section ')][1]",
                )
                selections = section.find_element(
                    By.XPATH,
                    ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                    "' matrix-selections ')]",
                )
                radios = selections.find_elements(
                    By.XPATH, ".//input[@type='radio']"
                )
                if not radios:
                    continue
                if any(radio.is_selected() for radio in radios):
                    continue

                if not selections.is_displayed():
                    try:
                        item.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", item)
                    sleep(0.2)

                ordered_radios = []
                for value in preferred_values or []:
                    ordered_radios.extend(
                        radio
                        for radio in radios
                        if radio.get_attribute("value") == value
                        and radio not in ordered_radios
                    )
                ordered_radios.extend(
                    radio for radio in radios if radio not in ordered_radios
                )

                for radio in ordered_radios:
                    try:
                        if not radio.is_enabled():
                            continue
                        choice = radio.find_element(By.XPATH, "./ancestor::li[1]")
                        if choice.get_attribute("data-disabled") == "true":
                            continue
                    except NoSuchElementException:
                        pass
                    except Exception:
                        continue

                    if self._click_radio_elem(driver, radio):
                        break
            except Exception:
                continue
        return True

    def _click_qnr_radio_groups(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> bool:
        """QNR形式のラジオを各設問の先頭候補から選択し、状態を確認する。"""
        radios = driver.find_elements(
            By.XPATH,
            "//ul[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-list ')]//input[@type='radio']",
        )
        if not radios:
            return False

        groups = {}
        for index, radio in enumerate(radios):
            try:
                name = radio.get_attribute("name")
            except Exception:
                name = None
            key = ("name", name) if name else ("unnamed", index)
            group = groups.setdefault(
                key, {"radios": [], "list_ids": set()}
            )
            group["radios"].append(radio)
            try:
                list_container = radio.find_element(
                    By.XPATH,
                    "./ancestor::ul[contains(concat(' ', normalize-space(@class), ' '), "
                    "' qnr-q-ch-list ')][1]",
                )
                group["list_ids"].add(list_container.id)
            except Exception:
                pass

        all_groups_selected = True
        for group_data in groups.values():
            group = group_data["radios"]
            if any(
                self._is_qnr_radio_selected(radio) for radio in group
            ):
                continue

            ordered_radios = []
            if len(group_data["list_ids"]) > 1 and len(group) > 1:
                ordered_radios.append(group[1])
            for value in preferred_values or []:
                ordered_radios.extend(
                    radio
                    for radio in group
                    if radio.get_attribute("value") == value
                    and radio not in ordered_radios
                )
            ordered_radios.extend(
                radio for radio in group if radio not in ordered_radios
            )

            for radio in ordered_radios:
                try:
                    if radio.is_enabled() and self._click_qnr_radio_elem(
                        driver, radio
                    ):
                        break
                except Exception:
                    continue
            else:
                all_groups_selected = False
                logging.warning(
                    "Could not select an option in a QNR single-answer group."
                )
        return all_groups_selected

    def _is_qnr_radio_selected(self, radio) -> bool:
        try:
            if radio.is_selected():
                return True
        except Exception:
            pass

        try:
            form = radio.find_element(
                By.XPATH,
                "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' qnr-q-ch-form ')][1]",
            )
            return "chk" in (form.get_attribute("class") or "").split()
        except Exception:
            return False

    def _click_qnr_radio_elem(
        self, driver: webdriver.Chrome, radio
    ) -> bool:
        """QNRのinputと表示クラスの両方でラジオ選択を確認する。"""
        if self._is_qnr_radio_selected(radio):
            return True

        targets = []
        try:
            radio_id = radio.get_attribute("id")
            if radio_id:
                targets.extend(
                    driver.find_elements(
                        By.XPATH, f"//label[@for='{radio_id}']"
                    )
                )
        except Exception:
            pass

        for xpath in (
            "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-form ')][1]",
            "./ancestor::li[1]",
        ):
            try:
                target = radio.find_element(By.XPATH, xpath)
                if target not in targets:
                    targets.append(target)
            except Exception:
                continue
        targets.append(radio)

        for target in targets:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target
                )
            except Exception:
                pass
            try:
                target.click()
            except Exception:
                try:
                    driver.execute_script(
                        "arguments[0].click();", target
                    )
                except Exception:
                    continue

            for _ in range(10):
                if self._is_qnr_radio_selected(radio):
                    return True
                sleep(0.1)
        return False

    def click_checkbox(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> None:
        try:
            if self._click_aria_checkbox_tiles(driver):
                return

            checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
            if len(checkboxes) == 0:
                return

            if self._click_ma_checkbox_choices(driver):
                return

            if self._click_qnr_matrix_checkbox_questions(
                driver, preferred_values=preferred_values
            ):
                return

            qnr_sections = driver.find_elements(
                By.XPATH,
                "//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' qnr-q-ch-section ')][.//ul[contains(concat(' ', "
                "normalize-space(@class), ' '), ' qnr-q-ch-list ')]"
                "//input[@type='checkbox']]",
            )
            if qnr_sections:
                for section in qnr_sections:
                    section_checkboxes = section.find_elements(
                        By.XPATH,
                        ".//ul[contains(concat(' ', normalize-space(@class), ' '), "
                        "' qnr-q-ch-list ')]//li[@data-choiceid and "
                        "not(@data-exclusive='true')]//input[@type='checkbox']",
                    )
                    try:
                        if any(
                            checkbox.is_selected()
                            for checkbox in section_checkboxes
                        ):
                            continue
                    except Exception:
                        pass

                    selected = False
                    for checkbox in section_checkboxes[:2]:
                        if self._click_qnr_checkbox(driver, checkbox):
                            selected = True
                            break
                    if not selected:
                        logging.warning(
                            "QNR checkbox section had no selected choice among "
                            "its first two options."
                        )
                return

            qnr_checkboxes = driver.find_elements(
                By.XPATH,
                "//ul[contains(concat(' ', normalize-space(@class), ' '), "
                "' qnr-q-ch-list ')]//li[@data-choiceid and "
                "not(@data-exclusive='true')]//input[@type='checkbox']",
            )
            if qnr_checkboxes:
                for checkbox in qnr_checkboxes[:2]:
                    if self._click_qnr_checkbox(driver, checkbox):
                        break
                return

            if self._click_checkbox_matrix_columns(driver):
                return

            if self._click_non_participation_checkbox(driver):
                return

            qnr_checkboxes = driver.find_elements(
                By.XPATH,
                "//ul[contains(concat(' ', normalize-space(@class), ' '), "
                "' qnr-q-ch-list ')]//input[@type='checkbox']",
            )
            if qnr_checkboxes:
                for checkbox in qnr_checkboxes:
                    if self._click_qnr_checkbox(driver, checkbox):
                        return
                return

            if self._click_labeled_checkbox_choices(driver, checkboxes):
                return

            if self._click_named_checkbox_choice(driver):
                return

            for idx, checkbox in enumerate(checkboxes):
                # 一番最後は排他設問のことが多いため、最後のボタンはクリックしない
                if idx == len(checkboxes) - 1:
                    break
                # 3の倍数値のボタンだけclickする
                elif idx % 3 == 0:
                    try:
                        checkbox.click()
                    except ElementNotInteractableException:
                        continue
                else:
                    continue
            sleep(1)
            # TODO
            tabindexes = driver.find_elements(By.XPATH, "//input[@tabindex]")

            for check_box in tabindexes:
                try:
                    check_box.click()
                except ElementClickInterceptedException:
                    continue

            index: int = 1
            for i in range(len(tabindexes)):
                index += 20 * i
                try:
                    driver.find_element(
                        By.XPATH, f"//input[@tabindex='{index}']"
                    ).click()
                except NoSuchElementException:
                    continue
        except Exception:
            logging.exception("Failed while selecting checkbox responses.")

    def _click_aria_checkbox_tiles(self, driver: webdriver.Chrome) -> bool:
        """itile形式のARIAチェックボックスタイルを設問ごとに1つ選択する。"""
        tables = driver.find_elements(
            By.XPATH,
            "//table[contains(concat(' ', normalize-space(@class), ' '), "
            "' itile_cells ')]",
        )
        if not tables:
            return False

        for table in tables:
            choices = table.find_elements(
                By.XPATH, ".//*[@role='checkbox']"
            )
            if not choices:
                continue
            try:
                if any(
                    choice.get_attribute("aria-checked") == "true"
                    for choice in choices
                ):
                    continue
            except Exception:
                pass

            for choice in choices:
                try:
                    if choice.get_attribute("aria-disabled") == "true":
                        continue
                    if choice.get_attribute("aria-checked") == "true":
                        break
                    cell = choice.find_element(
                        By.XPATH,
                        "./ancestor::td[contains(concat(' ', normalize-space(@class), ' '), "
                        "' itile_cate ')][1]",
                    )
                    if (
                        cell.get_attribute("aria-disabled") == "true"
                        or not cell.is_displayed()
                    ):
                        continue
                except Exception:
                    continue

                targets = [cell, choice]
                choice_id = choice.get_attribute("id")
                if choice_id:
                    targets.extend(
                        driver.find_elements(
                            By.XPATH, f"//label[@for='{choice_id}']"
                        )
                    )
                for target in targets:
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            target,
                        )
                    except Exception:
                        pass
                    try:
                        target.click()
                    except Exception:
                        try:
                            driver.execute_script(
                                "arguments[0].click();", target
                            )
                        except Exception:
                            continue

                    for _ in range(10):
                        try:
                            if choice.get_attribute("aria-checked") == "true":
                                break
                        except Exception:
                            break
                        sleep(0.1)
                    else:
                        continue
                    break
                else:
                    continue
                break
            else:
                logging.warning(
                    "No selectable option was activated in an itile checkbox table."
                )
        return True

    def _click_qnr_matrix_checkbox_questions(
        self,
        driver: webdriver.Chrome,
        preferred_values: Optional[List[str]] = None,
    ) -> bool:
        """折りたたみ式QNRマトリクスを設問ごとに開き、選択肢を選ぶ。"""
        matrix_containers = driver.find_elements(
            By.XPATH,
            "//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch ') and contains(concat(' ', normalize-space(@class), ' '), "
            "' qtype-matrix ')][.//input[@type='checkbox']]",
        )
        if not matrix_containers:
            return False

        section_xpath = (
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-section ')]"
        )
        item_xpath = (
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix-item ')]"
        )
        selections_xpath = (
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix-selections ')]"
        )
        ordered_values = [
            value
            for value in (preferred_values or [])
            if value in ("1", "2")
        ]
        ordered_values.extend(
            value for value in ("1", "2") if value not in ordered_values
        )

        for matrix in matrix_containers:
            for section in matrix.find_elements(By.XPATH, section_xpath):
                try:
                    items = section.find_elements(By.XPATH, item_xpath)
                    selections_list = section.find_elements(
                        By.XPATH, selections_xpath
                    )
                    if not items or not selections_list:
                        continue

                    item = items[0]
                    selections = selections_list[0]
                    if not selections.is_displayed():
                        try:
                            item.click()
                        except Exception:
                            driver.execute_script(
                                "arguments[0].click();", item
                            )
                        for _ in range(10):
                            if selections.is_displayed():
                                break
                            sleep(0.1)

                    checkboxes = selections.find_elements(
                        By.XPATH,
                        ".//ul[contains(concat(' ', normalize-space(@class), ' '), "
                        "' qnr-q-ch-list ')]//input[@type='checkbox']",
                    )
                    selected_checkboxes = [
                        checkbox
                        for checkbox in checkboxes
                        if checkbox.is_selected()
                    ]
                    preferred_value = ordered_values[0]
                    if any(
                        checkbox.get_attribute("value") == preferred_value
                        for checkbox in selected_checkboxes
                    ):
                        continue
                    for checkbox in selected_checkboxes:
                        if not self._set_qnr_checkbox_state(
                            driver, checkbox, selected=False
                        ):
                            logging.warning(
                                "Could not clear a prior QNR matrix selection."
                            )

                    candidates = []
                    for value in ordered_values:
                        candidates.extend(
                            checkbox
                            for checkbox in checkboxes
                            if checkbox.get_attribute("value") == value
                            and checkbox not in candidates
                        )

                    selected = False
                    for checkbox in candidates:
                        try:
                            if not checkbox.is_enabled() or not checkbox.is_displayed():
                                continue
                            choice = checkbox.find_element(
                                By.XPATH, "./ancestor::li[1]"
                            )
                            if (
                                choice.get_attribute("data-disabled") == "true"
                                or not choice.is_displayed()
                            ):
                                continue
                        except Exception:
                            continue

                        if self._click_qnr_checkbox(driver, checkbox):
                            selected = True
                            break

                    if not selected:
                        logging.warning(
                            "QNR matrix section had no selectable choice among "
                            "its first two options."
                        )
                except Exception:
                    logging.exception(
                        "Failed to select a QNR matrix checkbox section."
                    )
        return True

    def _qnr_matrix_page_signature(self, driver: webdriver.Chrome):
        """現在表示中のQNRマトリクスを識別し、遷移有無を判定する。"""
        matrix_items = driver.find_elements(
            By.XPATH,
            "//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch ') and contains(concat(' ', normalize-space(@class), ' '), "
            "' qtype-matrix ')][.//input[@type='checkbox']]"
            "//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' matrix-item ')]",
        )
        if not matrix_items:
            return None

        labels = []
        for item in matrix_items:
            try:
                label = item.find_element(
                    By.XPATH,
                    ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                    "' matrix-item-text ')]",
                ).text
                labels.append(
                    (item.get_attribute("data-sid"), " ".join(label.split()))
                )
            except Exception:
                return None
        try:
            return driver.current_url, tuple(labels)
        except Exception:
            return tuple(labels)

    def _click_ma_checkbox_choices(self, driver: webdriver.Chrome) -> bool:
        """独自表示コントロールを持つMA形式のcheckboxを1つ選択する。"""
        choices = driver.find_elements(
            By.XPATH,
            "//input[@type='checkbox' and "
            "contains(concat(' ', normalize-space(@class), ' '), ' MA ')]",
        )
        matched_choices = []
        for checkbox in choices:
            try:
                checkbox_id = checkbox.get_attribute("id")
                if not checkbox_id:
                    continue
                labels = driver.find_elements(
                    By.XPATH, f"//label[@for='{checkbox_id}']"
                )
                for label in labels:
                    controls = label.find_elements(
                        By.CSS_SELECTOR,
                        ".form-check-label[tabindex='0']",
                    )
                    if controls:
                        matched_choices.append((checkbox, controls[0], label))
                        break
            except Exception:
                continue

        for checkbox, control, label in matched_choices:
            try:
                if not checkbox.is_enabled():
                    continue
                if checkbox.is_selected():
                    return True
                checkbox_id = checkbox.get_attribute("id")
            except Exception:
                continue

            for target in (control, label, checkbox):
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", target
                    )
                except Exception:
                    pass
                try:
                    target.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", target)
                    except Exception:
                        continue

                for _ in range(10):
                    try:
                        current_checkbox = checkbox
                        if checkbox_id:
                            current_matches = driver.find_elements(
                                By.ID, checkbox_id
                            )
                            if current_matches:
                                current_checkbox = current_matches[0]
                        if current_checkbox.is_selected():
                            return True
                    except Exception:
                        pass
                    sleep(0.1)

        return False

    def _click_labeled_checkbox_choices(
        self, driver: webdriver.Chrome, checkboxes
    ) -> bool:
        """関連labelやカスタム表示で操作するcheckboxを先頭から選択する。"""
        for checkbox in checkboxes:
            try:
                checkbox_id = checkbox.get_attribute("id")
                labels = (
                    driver.find_elements(
                        By.XPATH, f"//label[@for='{checkbox_id}']"
                    )
                    if checkbox_id
                    else []
                )
                if not labels:
                    labels = checkbox.find_elements(
                        By.XPATH, "./ancestor::label[1]"
                    )
                if not labels:
                    continue
                if self._click_checkbox_input(driver, checkbox):
                    return True
            except Exception:
                continue
        return False

    def _click_qnr_checkbox(
        self, driver: webdriver.Chrome, checkbox
    ) -> bool:
        return self._set_qnr_checkbox_state(
            driver, checkbox, selected=True
        )

    def _set_qnr_checkbox_state(
        self, driver: webdriver.Chrome, checkbox, *, selected: bool
    ) -> bool:
        try:
            if not checkbox.is_enabled():
                return False
            if checkbox.is_selected() == selected:
                return True
            checkbox_id = checkbox.get_attribute("id")
        except Exception:
            return False

        targets = []
        if checkbox_id:
            targets.extend(
                driver.find_elements(By.XPATH, f"//label[@for='{checkbox_id}']")
            )
        for xpath in (
            "./ancestor::label[1]",
            "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-form ')][1]",
            "./ancestor::li[1]",
        ):
            try:
                target = checkbox.find_element(By.XPATH, xpath)
                if target not in targets:
                    targets.append(target)
            except Exception:
                continue
        targets.append(checkbox)

        for target in targets:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target
                )
            except Exception:
                pass
            try:
                target.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", target)
                except Exception:
                    continue

            for _ in range(10):
                try:
                    current_checkbox = checkbox
                    if checkbox_id:
                        current_matches = driver.find_elements(
                            By.ID, checkbox_id
                        )
                        if current_matches:
                            current_checkbox = current_matches[0]
                    if current_checkbox.is_selected() == selected:
                        return True
                except Exception:
                    pass
                sleep(0.1)

        try:
            current_checkbox = checkbox
            if checkbox_id:
                current_matches = driver.find_elements(
                    By.ID, checkbox_id
                )
                if current_matches:
                    current_checkbox = current_matches[0]
            driver.execute_script(
                """
                const checkbox = arguments[0];
                const selected = arguments[1];
                if (checkbox.checked !== selected) {
                    checkbox.checked = selected;
                    checkbox.dispatchEvent(new Event('input', { bubbles: true }));
                    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                current_checkbox,
                selected,
            )
            for _ in range(10):
                current_checkbox = checkbox
                if checkbox_id:
                    current_matches = driver.find_elements(
                        By.ID, checkbox_id
                    )
                    if current_matches:
                        current_checkbox = current_matches[0]
                if current_checkbox.is_selected() == selected:
                    return True
                sleep(0.1)
        except Exception:
            logging.exception("QNR checkbox state fallback failed.")
        return False

    def _click_checkbox_matrix_columns(self, driver: webdriver.Chrome) -> bool:
        """表形式のチェックボックスを列ごとに回答する。該当なしを優先する。"""
        no_match_phrases = (
            "あてはまるものはない",
            "当てはまるものはない",
            "あてはまるものがない",
            "当てはまるものがない",
            "該当するものはない",
            "該当するものはありません",
            "いずれも該当しない",
            "いずれにも該当しない",
            "どれも該当しない",
            "どれにも該当しない",
            "該当なし",
        )
        tables = {}
        for checkbox in driver.find_elements(
            By.XPATH, "//table//input[@type='checkbox']"
        ):
            try:
                row = checkbox.find_element(By.XPATH, "./ancestor::tr[1]")
                table = checkbox.find_element(By.XPATH, "./ancestor::table[1]")
                cell = checkbox.find_element(By.XPATH, "./ancestor::td[1]")
                cells = row.find_elements(By.XPATH, "./td|./th")
                column_index = next(
                    index for index, candidate in enumerate(cells) if candidate == cell
                )
                first_cell_text = (
                    " ".join(cells[0].text.split()) if cells else ""
                )
                table_rows = tables.setdefault(
                    table.id, {"columns": {}, "row_order": []}
                )
                row_id = row.id
                if row_id not in table_rows["row_order"]:
                    table_rows["row_order"].append(row_id)
                row_data = table_rows["columns"].setdefault(
                    row_id,
                    {"column_indices": set(), "checkboxes": {}, "text": first_cell_text},
                )
                row_data["column_indices"].add(column_index)
                row_data["checkboxes"][column_index] = checkbox
            except Exception:
                continue

        handled_matrix = False
        for table_data in tables.values():
            columns = sorted(
                {
                    column_index
                    for row_data in table_data["columns"].values()
                    for column_index in row_data["column_indices"]
                }
            )
            if len(columns) < 2:
                continue
            handled_matrix = True

            ordered_rows = [
                table_data["columns"][row_id]
                for row_id in table_data["row_order"]
            ]
            no_match_rows = [
                row_data
                for row_data in ordered_rows
                if any(
                    phrase in "".join(row_data["text"].split())
                    for phrase in no_match_phrases
                )
            ]

            for column_index in columns:
                column_choices = [
                    row_data["checkboxes"][column_index]
                    for row_data in ordered_rows
                    if column_index in row_data["checkboxes"]
                ]
                preferred_choices = [
                    row_data["checkboxes"][column_index]
                    for row_data in no_match_rows
                    if column_index in row_data["checkboxes"]
                ]
                ordered_choices = preferred_choices + [
                    checkbox
                    for checkbox in column_choices
                    if checkbox not in preferred_choices
                ]

                selected = False
                for checkbox in ordered_choices:
                    if self._click_checkbox_input(driver, checkbox):
                        selected = True
                        break
                    if preferred_choices and checkbox in preferred_choices:
                        break

                if not selected and preferred_choices:
                    for checkbox in column_choices:
                        if self._click_checkbox_input(driver, checkbox):
                            selected = True
                            break
        return handled_matrix

    def _click_checkbox_input(
        self, driver: webdriver.Chrome, checkbox
    ) -> bool:
        try:
            if not checkbox.is_enabled():
                return False
            if checkbox.is_selected():
                return True
        except Exception:
            return False

        targets = [checkbox]
        try:
            checkbox_id = checkbox.get_attribute("id")
            if checkbox_id:
                targets.extend(
                    driver.find_elements(By.XPATH, f"//label[@for='{checkbox_id}']")
                )
        except Exception:
            pass
        for xpath in (
            "./following-sibling::*[1]",
            "./ancestor::label[1]",
            "./ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' qnr-q-ch-form ')][1]",
            "./ancestor::li[1]",
        ):
            try:
                target = checkbox.find_element(By.XPATH, xpath)
                if target not in targets:
                    targets.append(target)
            except Exception:
                continue

        for target in targets:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target
                )
            except Exception:
                pass
            try:
                target.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", target)
                except Exception:
                    continue
            for _ in range(5):
                try:
                    if checkbox.is_selected():
                        return True
                except Exception:
                    break
                sleep(0.1)
        return False

    def _click_named_checkbox_choice(self, driver: webdriver.Chrome) -> bool:
        """名前付きの選択肢チェックボックスを先頭から一つ選択する。"""
        choices = driver.find_elements(
            By.XPATH, "//input[@type='checkbox' and @name='choice']"
        )
        for checkbox in choices:
            try:
                if not checkbox.is_enabled():
                    continue
                if checkbox.is_selected():
                    return True

                targets = [checkbox]
                try:
                    targets.append(
                        checkbox.find_element(By.XPATH, "./ancestor::label[1]")
                    )
                except Exception:
                    pass

                for target in targets:
                    try:
                        target.click()
                    except Exception:
                        try:
                            driver.execute_script(
                                "arguments[0].click();", target
                            )
                        except Exception:
                            continue
                    if checkbox.is_selected():
                        return True
            except Exception:
                continue
        return False

    def _click_non_participation_checkbox(self, driver: webdriver.Chrome) -> bool:
        """否定的な参加・応募選択肢を文言から探して選択する。"""
        choice_keywords = (
            "応募しない",
            "申し込まない",
            "参加しない",
            "参加を希望しない",
            "参加できない",
            "不参加",
            "欠席",
            "都合が合わない",
        )
        candidates = []
        for checkbox in driver.find_elements(
            By.XPATH, "//input[@type='checkbox']"
        ):
            try:
                checkbox_id = checkbox.get_attribute("id")
                if checkbox_id:
                    labels = driver.find_elements(
                        By.XPATH, f"//label[@for='{checkbox_id}']"
                    )
                else:
                    labels = checkbox.find_elements(
                        By.XPATH, "./ancestor::label[1]"
                    )
                label_text = " ".join(
                    " ".join(label.text.split()) for label in labels
                )
                match = next(
                    (
                        index
                        for index, keyword in enumerate(choice_keywords)
                        if keyword in label_text
                    ),
                    None,
                )
                if match is not None:
                    candidates.append((match, checkbox))
            except Exception:
                continue

        for _, checkbox in sorted(candidates, key=lambda candidate: candidate[0]):
            try:
                if not checkbox.is_enabled():
                    continue
                if checkbox.is_selected():
                    return True

                click_targets = [checkbox]
                checkbox_id = checkbox.get_attribute("id")
                if checkbox_id:
                    click_targets.extend(
                        driver.find_elements(
                            By.XPATH, f"//label[@for='{checkbox_id}']"
                        )
                    )
                try:
                    click_targets.append(
                        checkbox.find_element(
                            By.XPATH, "./ancestor::li[1]"
                        )
                    )
                except Exception:
                    pass

                for target in click_targets:
                    try:
                        target.click()
                    except Exception:
                        try:
                            driver.execute_script(
                                "arguments[0].click();", target
                            )
                        except Exception:
                            continue
                    if checkbox.is_selected():
                        return True
            except Exception:
                continue
        return False

    def _fill_birth_date_inputs(self, driver: webdriver.Chrome) -> List:
        """年・月・日で分かれた欄を単位表示から判別し、生年月日を入力する。"""
        date_groups = {}
        date_fields = []
        inputs = driver.find_elements(
            By.XPATH, "//input[@type='text' or @type='tel']"
        )
        for field in inputs:
            try:
                cell = field.find_element(By.XPATH, "./ancestor::td[1]")
                cell_text = cell.get_attribute("textContent") or cell.text
                cell_text = " ".join(cell_text.split())
                if "年" in cell_text:
                    part = "year"
                elif "月" in cell_text:
                    part = "month"
                elif "日" in cell_text:
                    part = "day"
                else:
                    continue

                date_fields.append(field)
                row = field.find_element(By.XPATH, "./ancestor::tr[1]")
                date_groups.setdefault(row.id, {})[part] = field
            except Exception:
                continue

        date_parts = (("year", "1985"), ("month", "8"), ("day", "2"))
        for group in date_groups.values():
            if not all(part in group for part, _ in date_parts):
                continue
            for part, value in date_parts:
                field = group[part]
                try:
                    if not field.is_enabled():
                        raise RuntimeError(f"{part} field is disabled")
                    field.clear()
                    field.send_keys(value)
                    if field.get_attribute("value") != value:
                        raise RuntimeError(f"{part} field did not accept its value")
                except Exception as error:
                    print(f"生年月日入力に失敗しました（{part}）: {error}")
                    break
        return date_fields

    def write_text(self, driver: webdriver.Chrome) -> None:
        _dummy_text = "31"
        filled = False
        birth_date_inputs = self._fill_birth_date_inputs(driver)
        try:
            text_forms = driver.find_elements(By.XPATH, "//input[@type='text']")
            for radio in text_forms:
                if any(radio == field for field in birth_date_inputs):
                    continue
                radio.send_keys(_dummy_text)
                filled = True
        except Exception:
            text_forms = []

        try:
            text_forms = driver.find_elements(By.XPATH, "//input[@type='tel']")
            for radio in text_forms:
                if any(radio == field for field in birth_date_inputs):
                    continue
                radio.send_keys("44")
                filled = True
        except Exception:
            pass

        if filled:
            sleep(1)

    def select_btn(self, driver: webdriver.Chrome) -> None:
        # dropdownが存在すれば先にclickしておく
        toggles = driver.find_elements(By.XPATH, "//a[@data-toggle='dropdown']")
        if len(toggles) > 0:
            for toggle in toggles:
                try:
                    toggle.click()
                    sleep(1)
                except ElementNotInteractableException:
                    print(
                        "//a[@data-toggle='dropdown']でElementNotInteractableExceptionが発生しました"
                    )
                    break
                except Exception:
                    break

        # dropdownが存在すれば先にclickしておく
        onfocus = driver.find_elements(By.XPATH, "//select[@onfocus]")
        if len(onfocus) > 0:
            for toggle in onfocus:
                try:
                    toggle.click()
                except ElementNotInteractableException:
                    print(
                        "//select[@onfocus]でElementNotInteractableExceptionが発生しました"
                    )
                    break
                except Exception:
                    break

        try:
            select_elems = driver.find_elements(By.XPATH, "//select")
            if len(select_elems) == 0:
                return

            for select_elem in select_elems:
                select = Select(select_elem)
                # 優先的に選びたい値の順
                preferred_values = ["1999", "09", "9"]
                chosen = False
                for pv in preferred_values:
                    try:
                        select.select_by_value(pv)
                        chosen = True
                        break
                    except Exception:
                        continue

                if not chosen:
                    # 2番目以降の有効な option を探して選択する。クリック不可ならJSで値を設定する
                    try:
                        options = select_elem.find_elements(By.TAG_NAME, "option")
                        idx = 0
                        for i, opt in enumerate(options):
                            try:
                                disabled = opt.get_attribute("disabled")
                            except Exception:
                                disabled = None
                            if disabled:
                                continue
                            # skip empty values which often are placeholder
                            val = opt.get_attribute("value") or ""
                            if val.strip() == "":
                                continue
                            idx = i
                            break
                        try:
                            select.select_by_index(idx)
                        except Exception:
                            # fallback: set via JS
                            try:
                                val = options[idx].get_attribute("value")
                                driver.execute_script(
                                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                                    select_elem,
                                    val,
                                )
                            except Exception:
                                continue
                    except Exception:
                        continue
            sleep(1)
        except Exception:
            # dropdown の操作で例外が出ても処理を継続する
            return

    def click_a_href(self, driver: webdriver.Chrome) -> None:
        # 新しいタブを増やすとリソース消費が増えるため、可能な限り同タブ遷移で処理する。
        # 外部ドメイン（pc.moppy.jp 以外）はスキップする。
        href_links = driver.find_elements(By.XPATH, "//a[@href]")
        if len(href_links) == 0:
            return

        for link in href_links:
            try:
                href = link.get_attribute("href")
            except Exception:
                continue
            if not href:
                continue
            href = href.strip()
            # 無効なhrefをスキップ
            if href.startswith("javascript:") or href.startswith("#"):
                continue

            # 相対パスは現在のURLを基に絶対URL化
            try:
                if href.startswith("/"):
                    href = urljoin(driver.current_url, href)
            except Exception:
                pass

            # 内部リンクのみ処理（ドメインに pc.moppy.jp を含むもの）
            try:
                parsed = urlparse(href)
                hostname = parsed.hostname or ""
            except Exception:
                hostname = ""

            if hostname in {
                "respondent.lupe.co.jp",
                "truffle.minedia.com",
            }:
                try:
                    if "アンケートに参加" not in link.text:
                        continue
                except Exception:
                    continue

                try:
                    button = link.find_element(By.XPATH, ".//button")
                    button.click()
                except Exception:
                    try:
                        link.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].click();", link)
                        except Exception:
                            continue
                return

            if "pc.moppy.jp" in hostname:
                try:
                    # 同タブで開いて戻る。これで新しいタブを大量に生成しない
                    driver.get(href)
                    sleep(1)
                    driver.back()
                except Exception:
                    continue
            else:
                # 外部ドメインはスキップ（重い外部コンテンツを開かない）
                continue

        # チェックボックス群は元の実装通り処理
        sleep(1)
        checkbox_elems = driver.find_elements(
            By.XPATH, "//input[@type='checkbox'][@tabindex]"
        )
        if len(checkbox_elems) == 0:
            return
        for idx, elem in enumerate(checkbox_elems):
            if idx == len(checkbox_elems) - 1:
                break
            try:
                elem.click()
            except Exception:
                continue

    def check_onclick_attr(self, driver: webdriver.Chrome) -> bool:
        try:
            onclick_btn = driver.find_element(By.XPATH, "//input[@type='submit']")
            onclick_btn.click()
            return True
        except NoSuchElementException:
            pass
        except ElementClickInterceptedException:
            return False
        except Exception:
            print("ブラウザで回答が推奨されるためskipします")

        try:
            onclick_buttons = driver.find_elements(By.XPATH, "//*[@onclick]")
            if len(onclick_buttons) == 0:
                pass
            for btn in onclick_buttons:
                try:
                    btn.click()
                except Exception:
                    continue
            return True
        except Exception:
            pass

        try:
            onclick_btn = driver.find_element(By.XPATH, "//*[id='next']")
            onclick_btn.click()
            return True
        except NoSuchElementException:
            pass
        except ElementClickInterceptedException:
            print("ブラウザで回答が推奨されるためskipします。")
        except Exception:
            return False

        return False

    def select_all_type_btn(
        self,
        driver: webdriver.Chrome,
        radio_values: Optional[List[str]] = None,
        checkbox_values: Optional[List[str]] = None,
    ) -> None:
        # radioボタン
        if radio_values:
            self.click_radio_with_values(driver, radio_values)
        else:
            self.click_radio(driver)
        # checkbox
        self.click_checkbox(
            driver,
            preferred_values=(
                checkbox_values if checkbox_values is not None else radio_values
            ),
        )
        # テキストフォーム
        self.write_text(driver)
        # selectボタン
        self.select_btn(driver)
        # リンク形式
        # 画面遷移に処理がかかるためリンク設問があるアンケートはskipする
        self.click_a_href(driver)
        # 動画再生
        self.play_video(driver)
        # 次へボタン
        next_button_xpaths = (
            "//a[contains(@class, 'btn')]",
            "//*[@name='next']",
            "//button[normalize-space(.)='次へ']",
        )
        for xpath in next_button_xpaths:
            for button in driver.find_elements(By.XPATH, xpath):
                try:
                    button.click()
                    return
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", button)
                        return
                    except Exception:
                        continue

    def play_video(self, driver: webdriver.Chrome) -> None:
        """動画の再生"""
        try:
            video_tags = driver.find_elements(By.TAG_NAME, "vide0")
            if len(video_tags) == 0:
                return

            for video_tag in video_tags:
                try:
                    driver.execute_script("arguments[0].click();", video_tag)
                    sleep(30)
                except Exception:
                    continue
        except Exception as e:
            print("====")
            print(e)
            print("====")
            print("動画再生でエラーが発生しました。")
            return

    def answer(self) -> None:
        urls = self.get_questionnaire_urls()
        sleep(1)
        driver = self._create_driver()
        # クッキーを追加
        self._load_cookies(driver)

        start = time.time()
        # 監視・再起動用の設定
        consecutive_overload = 0
        overload_check_threshold = 3  # 連続して超過した回数で再起動
        restart_count = 0
        max_restarts = 3

        for url in reversed(urls):
            elapsed_time = time.time()

            # 1時間30分以上経過すると自動で終了させる
            if elapsed_time - start > 60 * 90:
                break

            # skip URLs that were previously unable or already answered
            if url in self._unable_to_answer_urls or url in self._answered_urls:
                continue

            try:
                # 試すラジオの優先値のバリエーション
                variants = [["1"], ["2"], ["3"]]
                success = False
                matrix_retry_exhausted = False
                for radio_vals in variants:
                    matrix_checkbox_values = (
                        ["1"] if radio_vals[0] == "1" else ["2"]
                    )
                    matrix_second_choice_attempted = (
                        matrix_checkbox_values == ["2"]
                    )
                    print(f"{url}のアンケートを回答します")
                    driver.get(url)
                    if self._is_already_answered_page(driver):
                        print(f"既に回答済みまたは終了済みです: {url}")
                        self._add_url_to_list(url, collection="answered_urls")
                        success = True
                        break

                    # 初回ロード時に同意チェックボックスがあれば押す
                    try:
                        self.check_policy_checkbox(driver)
                    except Exception:
                        pass

                    # メールアドレス入力欄がある場合はスキップ
                    if self._has_email_question(driver):
                        print(
                            f"メールアドレスの入力が必要なためスキップしました: {url}"
                        )
                        self._add_url_to_list(url, collection="unable_to_answer_urls")
                        break

                    if self._click_survey_start_button(driver):
                        sleep(1)

                    answer_btn = driver.find_elements(By.XPATH, "//*[@onclick]")
                    if answer_btn:
                        for btn in answer_btn:
                            try:
                                btn.click()
                            except Exception:
                                continue

                    # マクロミル
                    answer_btns = driver.find_elements(By.NAME, "nextButton")
                    if answer_btns:
                        for answer_btn in answer_btns:
                            try:
                                answer_btn.click()
                            except Exception:
                                continue

                    # 同意ボタンをクリックする
                    self.check_policy_checkbox(driver)

                    has_onclick_attr: bool = True
                    sleep(2)
                    answer_count: int = 0
                    lock_detected = False
                    while has_onclick_attr:
                        matrix_signature_before = (
                            self._qnr_matrix_page_signature(driver)
                        )
                        self.select_all_type_btn(
                            driver,
                            radio_values=radio_vals,
                            checkbox_values=matrix_checkbox_values,
                        )
                        answer_count += 1
                        sleep(1)
                        has_onclick_attr = self.check_onclick_attr(driver)
                        if self._is_already_answered_page(driver):
                            print(f"回答終了画面を確認しました: {url}")
                            self._add_url_to_list(
                                url, collection="answered_urls"
                            )
                            success = True
                            break

                        matrix_signature_after = (
                            self._qnr_matrix_page_signature(driver)
                        )
                        if (
                            matrix_signature_before is not None
                            and matrix_signature_after == matrix_signature_before
                        ):
                            if not matrix_second_choice_attempted:
                                matrix_checkbox_values = ["2"]
                                matrix_second_choice_attempted = True
                                has_onclick_attr = True
                                continue
                            matrix_retry_exhausted = True
                            lock_detected = True
                            break
                        matrix_checkbox_values = ["1"]
                        matrix_second_choice_attempted = False

                        # ページに排他や混雑を示す要素が無いか検出
                        if self._detect_lock(driver):
                            lock_detected = True
                            break

                        # リソース監視: 過負荷検出
                        overloaded, cpu_p, mem_mb = self._check_driver_resource(driver)
                        if overloaded:
                            consecutive_overload += 1
                            if consecutive_overload >= overload_check_threshold:
                                restart_count += 1
                                print(
                                    f"リソース閾値超過を検出しました: {url} (cpu={cpu_p:.1f}%, mem={mem_mb:.1f}MB)。ドライバを再起動します。"
                                )
                                # 再起動回数上限を超えたらそのURLをスキップ
                                if restart_count > max_restarts:
                                    print(
                                        f"再起動上限に達したためスキップします: {url}"
                                    )
                                    self._add_url_to_list(url, collection="unable_to_answer_urls")
                                    lock_detected = True
                                    break

                                # driver を再作成・復旧
                                try:
                                    driver = self._restart_driver_preserve_cookies(
                                        driver
                                    )
                                except Exception:
                                    # 再起動失敗時はスキップ
                                    print(
                                        f"ドライバ再起動に失敗しました。{url} をスキップします。"
                                    )
                                    self._add_url_to_list(url, collection="unable_to_answer_urls")
                                    lock_detected = True
                                    break

                                consecutive_overload = 0
                                # 再起動後は現在のアンケートページへ戻って続行
                                continue
                        else:
                            consecutive_overload = 0

                        # 50回クリックする動作が発生した時回答を終了させる
                        if answer_count > 50:
                            break

                    if lock_detected:
                        # 違う回答バリエーションで再トライ
                        if matrix_retry_exhausted:
                            break
                        continue
                    if success:
                        break

                    try:
                        btn = driver.find_element(
                            By.XPATH, "//a[contains(@class, 'btn')]"
                        )
                        try:
                            btn.click()
                        except Exception:
                            pass
                    except NoSuchElementException:
                        pass
                    except Exception:
                        continue

                    checked_onclick_attr = self.check_onclick_attr(driver)
                    sleep(2)
                    if self._is_already_answered_page(driver):
                        print(f"回答終了画面を確認しました: {url}")
                        self._add_url_to_list(url, collection="answered_urls")
                        success = True
                        break
                    if checked_onclick_attr:
                        print("回答を完了しました！", url)
                        if url not in self._answered_urls:
                            self._add_url_to_list(url, collection="answered_urls")
                        success = True
                        break
                    else:
                        # 次のバリエーションで再試行
                        continue

                if not success:
                    print("回答を完了できなかったアンケート: ", url)
                    self._add_url_to_list(url, collection="unable_to_answer_urls")
            except ElementNotInteractableException:
                self._add_url_to_list(url, collection="unable_to_answer_urls")
            except Exception:
                print("回答を完了できなかったアンケート: ", url)
                self._add_url_to_list(url, collection="unable_to_answer_urls")
                continue

        driver.close()
        self._write_shared_csv()

    def answer_with_driver(
        self, driver: webdriver.Chrome, urls: List[str], worker_id: int = 0
    ) -> None:
        """既存のドライバを使ってアンケートに回答するロジック（`answer()`の内部ループを抽出）"""
        sleep(1)

        start = time.time()
        # 監視・再起動用の設定
        consecutive_overload = 0
        overload_check_threshold = 3
        restart_count = 0
        max_restarts = 3
        for url in reversed(urls):
            elapsed_time = time.time()

            # 1時間30分以上経過すると自動で終了させる
            if elapsed_time - start > 60 * 90:
                break

            # skip previously unable or already answered URLs
            if url in self._unable_to_answer_urls or url in self._answered_urls:
                continue

            self._mark_in_progress(url, worker_id)
            try:
                # 試すラジオの優先値のバリエーション
                variants = [["1"], ["2"], ["3"]]
                success = False
                matrix_retry_exhausted = False
                for radio_vals in variants:
                    matrix_checkbox_values = (
                        ["1"] if radio_vals[0] == "1" else ["2"]
                    )
                    matrix_second_choice_attempted = (
                        matrix_checkbox_values == ["2"]
                    )
                    self._worker_log(worker_id, f"{url}のアンケートを回答します")
                    driver.get(url)
                    if self._is_already_answered_page(driver):
                        self._worker_log(
                            worker_id, f"既に回答済みまたは終了済みです: {url}"
                        )
                        self._add_url_to_list(url, collection="answered_urls")
                        self._mark_done(url, worker_id, success=True)
                        success = True
                        break

                    # 初回ロード時に同意チェックボックスがあれば押す
                    try:
                        self.check_policy_checkbox(driver)
                    except Exception:
                        pass

                    # メールアドレス入力欄がある場合はスキップ
                    if self._has_email_question(driver):
                        self._worker_log(
                            worker_id,
                            f"メールアドレスの入力が必要なためスキップしました: {url}",
                        )
                        self._add_url_to_list(url, collection="unable_to_answer_urls")
                        self._mark_done(url, worker_id, success=False)
                        break

                    if self._click_survey_start_button(driver):
                        sleep(1)

                    answer_btn = driver.find_elements(By.XPATH, "//*[@onclick]")
                    if answer_btn:
                        for btn in answer_btn:
                            try:
                                btn.click()
                            except Exception:
                                continue

                    # macromill
                    answer_btns = driver.find_elements(By.NAME, "nextButton")
                    if answer_btns:
                        for answer_btn in answer_btns:
                            try:
                                answer_btn.click()
                            except Exception:
                                continue

                    # 同意ボタンをクリックする
                    self.check_policy_checkbox(driver)

                    has_onclick_attr: bool = True
                    sleep(2)
                    answer_count: int = 0
                    lock_detected = False
                    while has_onclick_attr:
                        matrix_signature_before = (
                            self._qnr_matrix_page_signature(driver)
                        )
                        self.select_all_type_btn(
                            driver,
                            radio_values=radio_vals,
                            checkbox_values=matrix_checkbox_values,
                        )
                        answer_count += 1
                        sleep(1)
                        has_onclick_attr = self.check_onclick_attr(driver)
                        if self._is_already_answered_page(driver):
                            self._worker_log(
                                worker_id, f"回答終了画面を確認しました: {url}"
                            )
                            self._add_url_to_list(
                                url, collection="answered_urls"
                            )
                            self._mark_done(url, worker_id, success=True)
                            success = True
                            break

                        matrix_signature_after = (
                            self._qnr_matrix_page_signature(driver)
                        )
                        if (
                            matrix_signature_before is not None
                            and matrix_signature_after == matrix_signature_before
                        ):
                            if not matrix_second_choice_attempted:
                                matrix_checkbox_values = ["2"]
                                matrix_second_choice_attempted = True
                                has_onclick_attr = True
                                continue
                            matrix_retry_exhausted = True
                            lock_detected = True
                            break
                        matrix_checkbox_values = ["1"]
                        matrix_second_choice_attempted = False

                        # ページに排他や混雑を示す要素が無いか検出
                        if self._detect_lock(driver):
                            lock_detected = True
                            break

                        # リソース監視
                        overloaded, cpu_p, mem_mb = self._check_driver_resource(driver)
                        if overloaded:
                            consecutive_overload += 1
                            if consecutive_overload >= overload_check_threshold:
                                restart_count += 1
                                self._worker_log(
                                    worker_id,
                                    f"リソース閾値超過を検出しました: {url} (cpu={cpu_p:.1f}%, mem={mem_mb:.1f}MB)。ドライバを再起動します。",
                                )
                                if restart_count > max_restarts:
                                    self._worker_log(
                                        worker_id,
                                        f"再起動上限に達したためスキップします: {url}",
                                    )
                                    self._add_url_to_list(url, collection="unable_to_answer_urls")
                                    self._mark_done(url, worker_id, success=False)
                                    lock_detected = True
                                    break
                                try:
                                    driver = self._restart_driver_preserve_cookies(
                                        driver
                                    )
                                except Exception:
                                    self._worker_log(
                                        worker_id,
                                        f"ドライバ再起動に失敗しました。{url} をスキップします。",
                                    )
                                    self._add_url_to_list(url, collection="unable_to_answer_urls")
                                    self._mark_done(url, worker_id, success=False)
                                    lock_detected = True
                                    break
                                consecutive_overload = 0
                                continue
                        else:
                            consecutive_overload = 0

                        # 50回クリックする動作が発生した時回答を終了させる
                        if answer_count > 50:
                            break

                    if lock_detected:
                        # 違う回答バリエーションで再トライ
                        if matrix_retry_exhausted:
                            break
                        continue
                    if success:
                        break

                    try:
                        btn = driver.find_element(
                            By.XPATH, "//a[contains(@class, 'btn')]"
                        )
                        try:
                            btn.click()
                        except Exception:
                            pass
                    except NoSuchElementException:
                        pass
                    except Exception:
                        continue

                    checked_onclick_attr = self.check_onclick_attr(driver)
                    sleep(2)
                    if self._is_already_answered_page(driver):
                        self._worker_log(
                            worker_id, f"回答終了画面を確認しました: {url}"
                        )
                        self._add_url_to_list(url, collection="answered_urls")
                        self._mark_done(url, worker_id, success=True)
                        success = True
                        break
                    if checked_onclick_attr:
                        self._worker_log(worker_id, f"回答を完了しました！ {url}")
                        if url not in self._answered_urls:
                            self._add_url_to_list(url, collection="answered_urls")
                        self._mark_done(url, worker_id, success=True)
                        success = True
                        break
                    else:
                        # 次のバリエーションで再試行
                        continue

                if not success:
                    self._worker_log(worker_id, f"回答を完了できなかったアンケート: {url}")
                    self._add_url_to_list(url, collection="unable_to_answer_urls")
                    self._mark_done(url, worker_id, success=False)
            except ElementNotInteractableException:
                self._add_url_to_list(url, collection="unable_to_answer_urls")
                self._mark_done(url, worker_id, success=False)
            except Exception:
                self._worker_log(worker_id, f"回答を完了できなかったアンケート: {url}")
                self._add_url_to_list(url, collection="unable_to_answer_urls")
                self._mark_done(url, worker_id, success=False)
                continue

        driver.close()
        self._write_shared_csv()

    def run(self) -> None:
        """1つのセッションでログインしてそのまま回答処理を進めるためのユーティリティメソッド"""
        # create driver and login
        self._option_add_argument()
        driver = webdriver.Chrome(executable_path=_DRIVER_PATH, options=self._options)
        driver.get(self._login_url)

        # login
        email_form = driver.find_element(By.XPATH, "//input[@name='mail']")
        email_form.send_keys(self._email)
        password_form = driver.find_element(By.XPATH, "//input[@name='pass']")
        password_form.send_keys(self._password)
        sleep(3)

        submit_button = driver.find_element(
            By.XPATH, "//button[@data-ga-label='ログイン']"
        )
        submit_button.submit()

        if self._check_success_login(driver):
            logging.info("login success!")
        else:
            logging.info("login failed!")

        # 移動してアンケート取得 → 回答 (同一ドライバを利用)
        urls = self.get_questionnaire_urls(driver=driver)
        try:
            self.answer_with_driver(driver, urls)
        except Exception:
            # answer_with_driver 内で driver.close() を呼ぶためここでは何もしない
            pass

    def _run_worker(self, worker_id: int, urls: List[str]) -> None:
        """単一のブラウザで特定のURL群をまとめて回答する。"""
        driver = self._create_driver()
        self._worker_log(worker_id, "ブラウザ起動")
        try:
            self._login_to_site(driver)
            self._worker_log(worker_id, "ログイン完了")
            self.answer_with_driver(driver, urls, worker_id=worker_id)
        except KeyboardInterrupt:
            self._worker_log(worker_id, "中断要求を受信しました")
            raise
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def clear_resume_state(self) -> None:
        self._in_progress_urls = []
        if os.path.exists(_PARALLEL_STATE_FILE):
            os.remove(_PARALLEL_STATE_FILE)

    def run_parallel(self, worker_count: int = 3) -> None:
        """3つ程度のブラウザワーカーを並列起動し、URLを分割して同時に回答する。"""
        if worker_count < 1:
            raise ValueError("worker_count は 1 以上でなければなりません")

        self._load_resume_state()
        urls = self.get_questionnaire_urls()
        if not urls:
            self._write_shared_csv()
            return

        pending_urls = [
            u
            for u in list(dict.fromkeys(self._in_progress_urls + urls))
            if u not in self._answered_urls and u not in self._unable_to_answer_urls
        ]

        if not pending_urls:
            self._write_shared_csv()
            return

        self._worker_log(0, f"{len(pending_urls)} 件のURLを {worker_count} worker で処理開始")
        batches = [[] for _ in range(worker_count)]
        for index, url in enumerate(pending_urls):
            batches[index % worker_count].append(url)

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(self._run_worker, worker_idx + 1, batch)
                    for worker_idx, batch in enumerate(batches)
                    if batch
                ]
                for future in futures:
                    future.result()
        except KeyboardInterrupt:
            self._save_resume_state()
            print("中断要求を受け取りました。次回の起動時に再開します。", flush=True)
            raise

        self._write_shared_csv()


def _write_unable_to_answer_urls(urls: List[str]) -> None:
    urls_list: List[List[str]] = []
    for url in urls:
        urls_list.append([url])
    with open(_UNABLE_TO_URL, "w") as f:
        writer = csv.writer(f)
        writer.writerows(urls_list)


def _write_answered_urls(urls: List[str]) -> None:
    urls_list: List[List[str]] = []
    for url in urls:
        urls_list.append([url])
    with open(_ANSWERED_URLS, "w") as f:
        writer = csv.writer(f)
        writer.writerows(urls_list)
