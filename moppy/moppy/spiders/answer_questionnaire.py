import logging
from time import sleep

import scrapy
from scrapy import FormRequest
from scrapy.selector import Selector
from scrapy_selenium import SeleniumRequest
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.select import Select

from ..config import EMAIL, PASSWORD


class AnswerQuestionnaireSpider(scrapy.Spider):
    name = "answer_questionnaire"

    def start_requests(self):
        url = "https://ssl.pc.moppy.jp/login/"
        yield SeleniumRequest(url=url, callback=self.login, wait_time=3)

    def login(self, response):
        driver = response.meta["driver"]
        email_form = driver.find_element(By.XPATH, "//input[@name='mail']")
        email_form.send_keys(EMAIL)
        password_form = driver.find_element(By.XPATH, "//input[@name='pass']")
        password_form.send_keys(PASSWORD)

        submit_button = driver.find_element(By.XPATH, "//button[@data-ga-label='ログイン']")

        submit_button.submit()

        sleep(3)

        html = driver.page_source
        selector = Selector(text=html)

        is_login = (
            selector.xpath("//a[@data-ga-label='ログアウト']/text()").get() is not None
        )
        logging.info(f"is_login: {is_login}")

        if is_login:
            questionnaire_url = selector.xpath(
                "//a[@data-ga-label='アンケート']/@href"
            ).get()

            yield SeleniumRequest(
                url=questionnaire_url,
                callback=self.get_able_to_answer_urls,
                wait_time=3,
            )
        else:
            raise ValueError("アンケートサイトの遷移に失敗しました")

    def get_able_to_answer_urls(self, response):
        driver = response.meta["driver"]

        html = driver.page_source
        selector = Selector(text=html)

        try:
            question_urls = list(
                selector.xpath("//li[@class='m-research__item']/a/@href").getall()
            )
        except Exception as e:
            driver.close()
            raise ValueError(e)

        if len(question_urls) == 0:
            raise ValueError("回答可能なアンケートが存在しませんでした")

        for url in question_urls:
            # url = question_urls[0]
            yield SeleniumRequest(url=url, callback=self.answer, wait_time=3)

    def answer(self, response):
        driver = response.meta["driver"]

        # 最初に「次へ」や「送信する」ボタンなどのonclick属性をもつ要素を取得
        onclick = self.check_onclick(driver)

        # 同意ボタンがある場合にクリックしておく
        self.answer_checkbox(driver)

        self.submit_btn(driver)
        driver = response.meta["driver"]

        # 次へボタンの確認
        is_next_btn = self.is_next_btn(driver)

        sleep(3)

        if is_next_btn:
            next_btn = driver.find_element(By.XPATH, "//input[@name='next']")
            next_btn.click()
            next_btn.submit()

        # ラジオボタン
        self.answer_radio_btn(driver)

        # 入力フォーム
        self.write_form(driver)

        # optionボタン
        self.select_option_btn(driver)

        self.click_all_btn_onclick_attr(response)

        self.submit_btn(driver)

        driver = response.meta["driver"]
        check_onclick_attr = self.check_onclick_attr(driver)
        if check_onclick_attr:
            print("======10======")
            self.answer(response)
        driver.close()

        logging.info("回答を終了しました")

        # try:
        #     self.answer(response)
        # except Exception as e:
        #     logging.info(e)
        #     driver.close()
        #     logging.info("回答を終了しました")

    def answer_checkbox(self, driver) -> None:
        try:
            checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
            for checkbox in checkboxes:
                checkbox.click()
        except Exception:
            logging.info("checkboxは存在しません。")

    def check_onclick(self, driver):
        try:
            onclick = driver.find_element(By.XPATH, "//*[@onclick]")
            onclick.click()
            return onclick
        except NoSuchElementException as e:
            logging.info(e)
            return None

    def click_all_btn_onclick_attr(self, response):
        driver = response.meta["driver"]
        try:
            onclick_buttons = driver.find_elements(By.XPATH, "//*[@onclick]")
            if len(onclick_buttons) > 0:
                for btn in onclick_buttons:
                    print(btn)
                    print("====")
                    btn.click()

        except NoSuchElementException as e:
            logging.info(e)
        except Exception as e:
            logging.info(e)

    def write_form(self, driver):
        try:
            text_areas = driver.find_elements(By.XPATH, "//textarea")
            if len(text_areas) > 0:
                for area in text_areas:
                    area.send_keys("test")
        except Exception as e:
            logging.info(e)
            logging.info("入力フォームは存在しませんでした。")

        try:
            text_input = driver.find_elements(By.XPATH, "//input[@type='text']")
            if len(text_input) > 0:
                for input in text_input:
                    input.send_keys("test")
        except Exception as e:
            logging.info(e)

    def select_option_btn(self, driver):
        try:
            select_area = driver.find_elements(By.XPATH, "//option")
            if len(select_area) > 0:
                for dropdown in select_area:
                    select = Select(dropdown)
                    select.select_by_index(1)  # 代表して1番目のoptionタグを選択状態に
        except Exception as e:
            logging.info(e)
            logging.info("セレクトボタンは存在しませんでした。")

    def answer_radio_btn(self, driver) -> None:
        radio_btn = driver.find_elements(By.XPATH, "//input[@type='radio']")
        if len(radio_btn) > 0:
            try:
                for radio in radio_btn:
                    # 全てのラジオボタンをチェックする
                    radio.send_keys(Keys.ENTER)
                    # radio.click()
                    sleep(1 / 2)
            except Exception as e:
                logging.info(e)
        else:
            logging.info("ラジオボタンは存在しませんでした。")

    def is_next_btn(self, driver) -> bool:
        try:
            next_btn = driver.find_element(By.XPATH, "//*[@name='next']")
        except Exception:
            next_btn = None
            logging.info("「次へ」ボタンが存在しません")

        return next_btn is not None

    def submit_btn(self, driver):
        try:
            submit_btn = driver.find_element(By.XPATH, "//input[@type='submit']")
            submit_btn.submit()
        except Exception as e:
            print("====")
            logging.info(e)
            print("====")

    def check_onclick_attr(self, driver):
        onclick_btn = driver.find_elements(By.XPATH, "//*[@onclick]")
        if len(onclick_btn) > 0:
            return True

        return False
