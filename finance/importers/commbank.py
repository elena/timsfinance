#!/usr/bin/python
#
# -*- coding: utf-8 -*-
# vim: set ts=4 sw=4 et sts=4 ai:

"""
Commonwealth Bank of Australia NetBank downloader.
"""

import csv
import os
import pdb
import time
import datetime
import re

from selenium.common.exceptions import TimeoutException, NoSuchElementException, NoSuchFrameException, ElementNotVisibleException
from selenium.webdriver.support.ui import WebDriverWait

from finance.importers.base import Importer
from finance import models

CURRENCY_MAP = {
    'CANADIAN DOLLAR': 'CAD',
    'US DOLLAR': 'USD',
    'POUND STERLING': 'GBP',
    }


class CommBankNetBank(Importer):

    def login(self, username, password):
        self.driver.get("https://www.my.commbank.com.au/netbank/Logon/Logon.aspx")
        WebDriverWait(self.driver, 10).until(lambda driver : driver.title.lower().startswith("netbank - logon"))

        usernamebox = self.driver.find_element_by_id("txtMyClientNumber_field")
        usernamebox.send_keys(username)

        passwordbox = self.driver.find_element_by_id("txtMyPassword_field")
        passwordbox.send_keys(password)

        form = self.driver.find_element_by_id("btnLogon_field")
        form.click()

        WebDriverWait(self.driver, 10).until(lambda driver: driver.title.lower().startswith("netbank - home"))
        return True

    @classmethod
    def _get_account_table(cls, driver):
      return driver.find_element_by_id("MyPortfolioGrid1_a")
     
    @classmethod
    def _account_table_found(cls, driver):
      try:
        cls._get_account_table(driver)
        return True
      except (NoSuchElementException, NoSuchFrameException), e:
        return False

    @classmethod
    def _account_table_rows(cls, driver):
        account_table = cls._get_account_table(driver)
        return account_table.find_elements_by_xpath('tbody/tr')[:-3]

    def home(self):
        WebDriverWait(self.driver, 10).until(self._account_table_found)
        time.sleep(5)

    def accounts(self, site, dummy=[]):
        if dummy:
            account_table_rows = dummy
        else:
            account_table_rows = self._account_table_rows(self.driver)

        account_details = []
        for account_row in account_table_rows:
            account_details.append([y.text.strip() for y in account_row.find_elements_by_xpath('td')])

        accounts = []
        for (description, _, account_id, balance, funds) in account_details:
            try:
                account = models.Account.objects.get(site=site, account_id=account_id)
            except models.Account.DoesNotExist:
                account = models.Account(site=site, account_id=account_id)
                account.imported_last = datetime.datetime.fromtimestamp(0)

            account.description = description
            account.currency = models.Currency.objects.get(pk="AUD")

            accounts.append(account)

        return accounts

    @classmethod
    def _get_export_select(cls, driver):
        return driver.find_element_by_id('ctl00_BodyPlaceHolder_blockExport_ddlExportType_field')

    @classmethod
    def _export_select_found(cls, driver):
        try:
            export_select = cls._get_export_select(driver)
            export_select.find_element_by_xpath('option').click()
            return True
        except (NoSuchElementException, NoSuchFrameException, ElementNotVisibleException), e:
            return False

    def transactions(self, account, start_date, end_date):
        for account_row in self._account_table_rows(self.driver):
            account_name = account_row.find_elements_by_xpath('td')[2].text.strip()
            if account_name == account.account_id:
                break
        else:
            raise AccountNotFound('Could not find account of id %s' % account_id)

        account_row.find_element_by_tag_name('a').click()

        WebDriverWait(self.driver, 10).until(lambda driver: driver.title.lower().startswith("netbank - trans"))
        time.sleep(5)

        show_form = self.driver.find_element_by_id('lnkShowHideSearch')
        show_form.click()
        time.sleep(5)

        date_range = self.driver.find_element_by_id('ctl00_BodyPlaceHolder_blockDates_rbtnChooseDates_field')
        date_range.click()
        time.sleep(5)

        from_date = self.driver.find_element_by_id('ctl00_BodyPlaceHolder_blockDates_caltbFrom_field')
        for key in start_date.strftime('%d/%m/%Y'):
            from_date.send_keys(key)
        time.sleep(5)
        to_date = self.driver.find_element_by_id('ctl00_BodyPlaceHolder_blockDates_caltbTo_field')
        for key in end_date.strftime('%d/%m/%Y'):
            to_date.send_keys(key)
        time.sleep(5)

        submit_button = self.driver.find_element_by_xpath('//input[@value="SEARCH"]')
        submit_button.click()

        WebDriverWait(self.driver, 10).until(self._export_select_found)
        time.sleep(5)

        export_select = self._get_export_select(self.driver)
        export_csv = export_select.find_element_by_xpath('option[@value="CSV"]')
        export_csv.click()

        export_button = self.driver.find_element_by_xpath('//input[@value="EXPORT TRANSACTIONS"]')
        export_button.click()

        time.sleep(5)

        for handle in self._get_files():
            return self.parse_file(account, handle)

    def parse_file(self, account, handle):
        transactions = []
        #                     0                        | location   ##1011               
        # 18/10/2011,"+9.23","PREMIUM TOURS LTD        LONDON  N1 0 ##1011           6.00 POUND STERLING",""
        for entered_date, amount, mangled_desc, _ in csv.reader(handle):
            trans_id = "%s|%s|%s" % (entered_date, amount, mangled_desc)

            try:
                trans = models.Transaction.objects.get(account=account, trans_id=trans_id)
            except models.Transaction.DoesNotExist:
                trans = models.Transaction(account=account, trans_id=trans_id)

            trans.imported_entered_date = datetime.datetime.strptime(entered_date, '%d/%m/%Y')
            trans.imported_description = mangled_desc[:25].strip()

            # Remove the decimal so we get back to cents
            amount = amount.replace('.', '')
            trans.imported_amount = int(amount)

            # Commbank reports extra info in the description about any currency
            # conversion that happened. Lets try and extract that info.
            extra_info = mangled_desc[25:].strip()
            if len(extra_info) > 0:
                if '##1011' in extra_info:
                    location, currency_info = [x.strip() for x in extra_info.split('##1011')]

                    # Get the location info
                    trans.imported_location = location.strip()

                    # Extract the currency info
                    stuff = re.match("([0-9]*.[0-9][0-9]) (.*)", currency_info)
                    if stuff:
                        amount, currency_type = stuff.groups()
                        amount = amount.replace('.', '')
                        
                        trans.imported_original_amount = int(amount)
                        trans.imported_original_currency = models.Currency.objects.get(pk=CURRENCY_MAP[currency_type])
                else:
                    trans.imported_location = extra_info + ', Australia'
            else:
                trans.imported_location = 'Australia'
           
            transactions.append(trans)
        return transactions
