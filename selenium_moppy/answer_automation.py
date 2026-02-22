import csv
import logging
import os
import pickle
import time
from pathlib import Path
from time import sleep

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.select import Select
from typing import Optional, List
from urllib.parse import urlparse, urljoin

_CURRENT_DIR = Path(__file__).absolute().parent.parent
_DRIVER_PATH = _CURRENT_DIR / "driver/chromedriver"
cookies_file = _CURRENT_DIR / "moppy.pkl"  # クッキーを保存するファイルの名前
_UNABLE_TO_URL = _CURRENT_DIR / "unable_to_answer.csv"

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

        with open(_UNABLE_TO_URL, "r") as f:
            reader = csv.reader(f)
            unable_to_answer_urls = [url[0] for url in reader]

        self._unable_to_answer_urls = unable_to_answer_urls

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

        return webdriver.Chrome(executable_path=_DRIVER_PATH, options=self._options)

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

    def save_cookie_as_pickle(self) -> None:
        """一度ログインしてcookieを保存、その後cookieを保持してアンケート画面へ遷移する"""
        if os.path.exists(self._cookie_file):
            os.remove(self._cookie_file)

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
        return urls

    def check_policy_checkbox(self, driver: webdriver.Chrome) -> None:
        try:
            policy_checkboxes = driver.find_elements(
                By.XPATH, "//input[@type='checkbox']"
            )
            if len(policy_checkboxes) == 0:
                return
            for policy_checkbox in policy_checkboxes:
                try:
                    policy_checkbox.click()
                except Exception:
                    continue

        except Exception:
            return

    def click_radio(self, driver: webdriver.Chrome) -> None:
        # デフォルトではvalue='1'を優先する
        try:
            radio_buttons = driver.find_elements(
                By.XPATH, "//input[@type='radio'][@value='1']"
            )
            if len(radio_buttons) == 0:
                # value=1が無ければ最初に見つかったラジオをクリックする
                radios = driver.find_elements(By.XPATH, "//input[@type='radio']")
                for r in radios:
                    try:
                        r.click()
                    except ElementNotInteractableException:
                        continue
                return

            for radio in radio_buttons:
                try:
                    radio.click()
                except ElementNotInteractableException:
                    continue
        except Exception:
            pass

    def click_radio_with_values(
        self, driver: webdriver.Chrome, values: List[str]
    ) -> None:
        """ラジオボタンをグループごとに優先値で選択する
        values は優先的に選ぶ value 属性のリスト
        """
        try:
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
                        try:
                            elem.click()
                            clicked = True
                            break
                        except ElementNotInteractableException:
                            continue
                    except NoSuchElementException:
                        continue

                if not clicked:
                    # 候補のいずれもクリックできなければ、そのグループの最初の選択肢をクリック
                    try:
                        first_elem = driver.find_element(
                            By.XPATH, f"//input[@type='radio' and @name='{name}']"
                        )
                        try:
                            first_elem.click()
                        except ElementNotInteractableException:
                            continue
                    except Exception:
                        continue
        except Exception:
            return

    def click_checkbox(self, driver: webdriver.Chrome) -> None:
        try:
            checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
            if len(checkboxes) == 0:
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
            return

    def write_text(self, driver: webdriver.Chrome) -> None:
        _dummy_text = "31"
        try:
            text_forms = driver.find_elements(By.XPATH, "//input[@type='text']")
            if len(text_forms) == 0:
                return
            for radio in text_forms:
                radio.send_keys(_dummy_text)
            sleep(1)
            return
        except Exception:
            pass

        try:
            text_forms = driver.find_elements(By.XPATH, "//input[@type='tel']")
            if len(text_forms) == 0:
                return
            for radio in text_forms:
                radio.send_keys(_dummy_text)
            sleep(1)
            return
        except Exception:
            return

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
        self, driver: webdriver.Chrome, radio_values: Optional[List[str]] = None
    ) -> None:
        # radioボタン
        if radio_values:
            self.click_radio_with_values(driver, radio_values)
        else:
            self.click_radio(driver)
        # checkbox
        self.click_checkbox(driver)
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
        try:
            try:
                btn = driver.find_element(By.XPATH, "//a[contains(@class, 'btn')]")
                btn.click()
            except NoSuchElementException:
                btn = driver.find_element(By.XPATH, "//*[@name='next']")
                btn.click()
        except NoSuchElementException:
            return
        except Exception:
            return

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
        for url in reversed(urls):
            elapsed_time = time.time()

            # 1時間30分以上経過すると自動で終了させる
            if elapsed_time - start > 60 * 90:
                break

            if url in self._unable_to_answer_urls:
                continue

            try:
                # 試すラジオの優先値のバリエーション
                variants = [["1"], ["2"], ["3"]]
                success = False
                for radio_vals in variants:
                    print(f"{url}のアンケートを回答します")
                    driver.get(url)

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
                        self.select_all_type_btn(driver, radio_values=radio_vals)
                        answer_count += 1
                        sleep(1)
                        has_onclick_attr = self.check_onclick_attr(driver)

                        # ページに排他や混雑を示す要素が無いか検出
                        if self._detect_lock(driver):
                            lock_detected = True
                            break

                        # 50回クリックする動作が発生した時回答を終了させる
                        if answer_count > 50:
                            break

                    if lock_detected:
                        # 違う回答バリエーションで再トライ
                        continue

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
                    if checked_onclick_attr:
                        print("回答を完了しました！", url)
                        success = True
                        break
                    else:
                        # 次のバリエーションで再試行
                        continue

                if not success:
                    print("回答を完了できなかったアンケート: ", url)
                    self._unable_to_answer_urls.append(url)
            except ElementNotInteractableException:
                self._unable_to_answer_urls.append(url)
            except Exception:
                print("回答を完了できなかったアンケート: ", url)
                self._unable_to_answer_urls.append(url)
                continue

        driver.close()
        _write_unable_to_answer_urls(self._unable_to_answer_urls)

    def answer_with_driver(self, driver: webdriver.Chrome, urls: List[str]) -> None:
        """既存のドライバを使ってアンケートに回答するロジック（`answer()`の内部ループを抽出）"""
        sleep(1)

        start = time.time()
        for url in reversed(urls):
            elapsed_time = time.time()

            # 1時間30分以上経過すると自動で終了させる
            if elapsed_time - start > 60 * 90:
                break

            if url in self._unable_to_answer_urls:
                continue

            try:
                # 試すラジオの優先値のバリエーション
                variants = [["1"], ["2"], ["3"]]
                success = False
                for radio_vals in variants:
                    print(f"{url}のアンケートを回答します")
                    driver.get(url)

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
                        self.select_all_type_btn(driver, radio_values=radio_vals)
                        answer_count += 1
                        sleep(1)
                        has_onclick_attr = self.check_onclick_attr(driver)

                        # ページに排他や混雑を示す要素が無いか検出
                        if self._detect_lock(driver):
                            lock_detected = True
                            break

                        # 50回クリックする動作が発生した時回答を終了させる
                        if answer_count > 50:
                            break

                    if lock_detected:
                        # 違う回答バリエーションで再トライ
                        continue

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
                    if checked_onclick_attr:
                        print("回答を完了しました！", url)
                        success = True
                        break
                    else:
                        # 次のバリエーションで再試行
                        continue

                if not success:
                    print("回答を完了できなかったアンケート: ", url)
                    self._unable_to_answer_urls.append(url)
            except ElementNotInteractableException:
                self._unable_to_answer_urls.append(url)
            except Exception:
                print("回答を完了できなかったアンケート: ", url)
                self._unable_to_answer_urls.append(url)
                continue

        driver.close()
        _write_unable_to_answer_urls(self._unable_to_answer_urls)

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


def _write_unable_to_answer_urls(urls: List[str]) -> None:
    urls_list: List[List[str]] = []
    for url in urls:
        urls_list.append([url])
    with open(_UNABLE_TO_URL, "w") as f:
        writer = csv.writer(f)
        writer.writerows(urls_list)
