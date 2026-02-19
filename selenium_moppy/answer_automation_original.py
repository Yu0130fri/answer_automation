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

_CURRENT_DIR = Path(__file__).absolute().parent.parent
_DRIVER_PATH = _CURRENT_DIR / "driver/chromedriver"
cookies_file = _CURRENT_DIR / "moppy.pkl"  # クッキーを保存するファイルの名前
_UNABLE_TO_URL = _CURRENT_DIR / "unable_to_answer.csv"

login_url = "https://ssl.pc.moppy.jp/login/"
questionnaire_url = "https://pc.moppy.jp/research/"

options = Options()
options.add_argument("--headless")


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

    def get_questionnaire_urls(self) -> list[str]:
        """アンケートURLを全て取得する（DBやjsonなどに保存するかは検討中）"""
        self._option_add_argument()
        driver = webdriver.Chrome(executable_path=_DRIVER_PATH, options=self._options)

        cookies = pickle.load(open(cookies_file, "rb"))
        driver.get(login_url)

        # login and add cookie
        for c in cookies:
            driver.add_cookie(c)
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
            count_eligible_questionnaire = int(
                count_eligible_questionnaire_str
            )
        except ValueError:
            count_eligible_questionnaire = 30

        count_scroll = count_eligible_questionnaire // 5
        for _ in range(count_scroll):
            try:
                scroll_btn = driver.find_element(
                    By.CLASS_NAME, 'a-btn__more--down'
                )
                scroll_btn.click()
                sleep(2)
            except Exception:
                break

        able_to_answer_urls = driver.find_elements(
            By.XPATH, "//a[contains(@href,'r_id')]"
        )

        if len(able_to_answer_urls) == 0:
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
        try:
            radio_buttons = driver.find_elements(
                By.XPATH, "//input[@type='radio'][@value='1']"
            )
            if len(radio_buttons) == 0:
                return

            for radio in radio_buttons:
                try:
                    radio.click()
                except ElementNotInteractableException:
                    continue
        except Exception:
            pass

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
                try:
                    select.select_by_value("1999")  # 年代のdropdown
                except Exception:
                    select.select_by_index(1)  # 代表して2番目の要素を選択

                try:
                    select.select_by_value("09")  # 月（日）のdropdown
                except Exception:
                    select.select_by_index(1)  # 代表して2番目の要素を選択

                try:
                    select.select_by_value("9")  # 月（日）のdropdown
                except Exception:
                    select.select_by_index(1)  # 代表して2番目の要素を選択
            sleep(1)

        except Exception:
            print("Exception raised in dropdown")

    def click_a_href(self, driver: webdriver.Chrome) -> None:
        # _blankでタブが変更してしまうときの対策
        main_tab = driver.current_window_handle
        href_links = driver.find_elements(By.XPATH, "//a[@href]")
        if len(href_links) == 0:
            return
        for link in href_links:
            sleep(1)
            link.send_keys(Keys.CONTROL, Keys.ENTER)
        sleep(1)
        driver.switch_to.window(main_tab)

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

    def select_all_type_btn(self, driver: webdriver.Chrome) -> None:
        # radioボタン
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
        driver = webdriver.Chrome(executable_path=_DRIVER_PATH, options=self._options)
        driver.get(login_url)

        cookies = pickle.load(open(cookies_file, "rb"))
        # login and add cookie
        for c in cookies:
            driver.add_cookie(c)

        start = time.time()
        for url in reversed(urls):
            elapsed_time = time.time()

            # 1時間30分以上経過すると自動で終了させる
            if elapsed_time - start > 60 * 90:
                break

            if url in self._unable_to_answer_urls:
                continue

            try:
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
                        answer_btn.click()

                # 同意ボタンをクリックする
                self.check_policy_checkbox(driver)

                has_onclick_attr: bool = True
                sleep(2)
                answer_count: int = 0
                while has_onclick_attr:
                    self.select_all_type_btn(driver)
                    answer_count += 1
                    sleep(1)
                    has_onclick_attr = self.check_onclick_attr(driver)

                    # 40回クリックする動作が発生した時回答を終了させる
                    if answer_count > 50:
                        break

                try:
                    btn = driver.find_element(By.XPATH, "//a[contains(@class, 'btn')]")
                    btn.click()
                except NoSuchElementException:
                    pass
                except Exception:
                    continue

                checked_onclick_attr = self.check_onclick_attr(driver)
                sleep(2)
                if checked_onclick_attr:
                    print("回答を完了しました！", url)
                else:
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


def _write_unable_to_answer_urls(urls: list[str]) -> None:
    urls_list: list[list[str]] = []
    for url in urls:
        urls_list.append([url])
    with open(_UNABLE_TO_URL, "w") as f:
        writer = csv.writer(f)
        writer.writerows(urls_list)
