import csv
import urllib.parse
from datetime import date, timedelta
from math import ceil
from typing import List, Optional, Dict, Any, Iterator

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from src.browser import (
    BrowserDriver,
    fetch_page,
    extract_html_table_rows,
    PageCheckFailedError,
    ResultsTableNotFoundError,
)
from src.utils import try_or_none, split_date_range_in_n

BASE_URL = "https://www.sec.gov/edgar/search/#/"

FILING_CATEGORIES_MAPPING = {
    "all_except_section_16": "form-cat0",
    "all_annual_quarterly_and_current_reports": "form-cat1",
    "all_section_16": "form-cat2",
    "beneficial_ownership_reports": "form-cat3",
    "exempt_offerings": "form-cat4",
    "registration_statements": "form-cat5",
    "filing_review_correspondence": "form-cat6",
    "sec_orders_and_notices": "form-cat7",
    "proxy_materials": "form-cat8",
    "tender_offers_and_going_private_tx": "form-cat9",
    "trust_indentures": "form-cat10",
}

RESULTS_TABLE_SELECTOR = "/html/body/div[3]/div[2]/div[2]/table/tbody"
DEFAULT_BATCHES_NUMBER = 2


class SecEdgarScraper:

    def __init__(self, driver: BrowserDriver):
        self.search_requests = []
        self.driver = driver

    def _parse_number_of_results(self) -> int:
        num_results = int(self.driver.find_element(By.ID, "show-result-count").text.replace(",", "").split(" ")[0])
        return num_results

    def _compute_number_of_pages(self) -> int:
        num_results = self._parse_number_of_results()
        num_pages = ceil(num_results / 100)
        print(f"Found {num_results} / 100 = {num_pages} pages")
        return num_pages

    @staticmethod
    def _parse_table_rows(rows: List[WebElement]) -> List[dict]:
        """
        Parses the given list of table rows into a list of dictionaries.

        :param rows: List of table rows to parse
        :return: List of dictionaries representing the parsed table rows
        """

        parsed_rows = []
        for r in rows:
            file_link_tag = try_or_none(
                lambda row: row.find_element(By.CLASS_NAME, "file-num").find_element(By.TAG_NAME, "a"))(r)
            filing_type = try_or_none(lambda row: row.find_element(By.CLASS_NAME, "filetype"))(r)
            filing_type_link = filing_type.find_element(By.CLASS_NAME, "preview-file")
            cik = try_or_none(
                lambda row: row.find_element(By.CLASS_NAME, "cik").get_attribute("innerText").split(" ")[1])(
                r)
            cik_cleaned = cik.strip("0")
            data_adsh = filing_type_link.get_attribute("data-adsh")
            data_adsh_no_dash = data_adsh.replace("-", "")
            data_file_name = filing_type_link.get_attribute("data-file-name")
            filing_details_url = f"https://www.sec.gov/Archives/edgar/data/{cik_cleaned}/{data_adsh_no_dash}/{data_adsh}-index.html"
            filing_doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_cleaned}/{data_adsh_no_dash}/{data_file_name}"
            parsed_rows.append(
                {
                    "filing_type": filing_type.text,
                    "filed_at": try_or_none(lambda row: row.find_element(By.CLASS_NAME, "filed").text)(r),
                    "reporting_for": try_or_none(lambda row: row.find_element(By.CLASS_NAME, "enddate").text)(r),
                    "entity_name": try_or_none(lambda row: row.find_element(By.CLASS_NAME, "entity-name").text)(r),
                    "company_cik": cik,
                    "place_of_business": try_or_none(
                        lambda row: row.find_element(By.CLASS_NAME, "biz-location").get_attribute("innerText"))(r),
                    "incorporated_location": try_or_none(
                        lambda row: row.find_element(By.CLASS_NAME, "incorporated").get_attribute("innerText"))(r),
                    "file_num": try_or_none(lambda row: file_link_tag.get_attribute("innerText"))(r),
                    "film_num": try_or_none(
                        lambda row: row.find_element(By.CLASS_NAME, "film-num").get_attribute("innerText"))(r),
                    "file_num_search_url": try_or_none(lambda row: file_link_tag.get_attribute("href"))(r),
                    "filing_details_url": filing_details_url,
                    "filing_document_url": filing_doc_url
                }
            )
        return parsed_rows

    @staticmethod
    def _generate_request_args(
            search_keywords: List[str],
            entity_identifier: Optional[str],
            filing_category: Optional[str],
            exact_search: bool,
            start_date: date,
            end_date: date,
            page_number: int,
    ) -> str:
        """
        Generates the request arguments for the SEC website based on the given parameters.

        :param search_keywords: Search keywords to input in the "Document word or phrase" field
        :param entity_identifier: Entity/Person name, ticker, or CIK number to input in the "Company name, ticker, or CIK" field
        :param filing_category: Filing category to select from the dropdown menu, defaults to None
        :param exact_search: Whether to perform an exact search on the search_keywords argument or not, defaults to False in order to return the maximum amount of search results by default
        :param start_date: Start date for the custom date range, defaults to 5 years ago to replicate the default behavior of the SEC website
        :param end_date: End date for the custom date range, defaults to current date in order to replicate the default behavior of the SEC website
        :param page_number: Page number to request, defaults to 1

        :return: URL-encoded request arguments string to concatenate to the SEC website URL
        """

        # Check that start_date is not after end_date
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        # Join search keywords into a single string
        search_keywords = " ".join(search_keywords)
        search_keywords = f'"{search_keywords}"' if exact_search else search_keywords

        # Generate request arguments
        request_args = {
            "q": urllib.parse.quote(search_keywords),
            "dateRange": "custom",
            "startdt": start_date.strftime("%Y-%m-%d"),
            "enddt": end_date.strftime("%Y-%m-%d"),
            "page": page_number,
        }

        # Add optional parameters
        if entity_identifier:
            request_args["entityName"] = entity_identifier
        if filing_category:
            request_args["category"] = FILING_CATEGORIES_MAPPING[filing_category]

        # URL-encode the request arguments
        request_args = urllib.parse.urlencode(request_args)

        return request_args

    def _fetch_search_request_results(
            self,
            search_request: str,
            wait_for_request_secs: int,
            stop_after_n: int,
    ) -> Iterator[Iterator[Dict[str, Any]]]:
        """
        Fetches the results for the given search request and paginates through the results.

        :param search_request: URL-encoded request arguments string to concatenate to the SEC website URL
        :param wait_for_request_secs: amount of time to wait for the request to complete
        :param stop_after_n: number of times to retry the request before failing
        :return: Iterator of dictionaries representing the parsed table rows
        """

        # Fetch first page, verify that the request was successful by checking the results table appears on the page
        fetch_page(
            self.driver, f"{BASE_URL}{search_request}", wait_for_request_secs, stop_after_n
        )(lambda: self.driver.find_element(By.XPATH, RESULTS_TABLE_SELECTOR).text.strip() != "")

        # Get number of pages
        num_pages = self._compute_number_of_pages()

        for i in range(1, num_pages + 1):
            paginated_url = f"{BASE_URL}{search_request}&page={i}"
            try:
                fetch_page(
                    self.driver, paginated_url, wait_for_request_secs, stop_after_n
                )(lambda: self.driver.find_element(By.XPATH, RESULTS_TABLE_SELECTOR).text.strip() != "")

                page_results = extract_html_table_rows(
                    self.driver, By.XPATH, RESULTS_TABLE_SELECTOR
                )(self._parse_table_rows)
                yield page_results
            except PageCheckFailedError as e:
                print(
                    f"Failed to fetch page at URL {paginated_url}, skipping..."
                )
                print(f"Error: {e}")
                continue
            except ResultsTableNotFoundError as e:
                print(
                    f"Did not find results table at URL {paginated_url}, skipping..."
                )
                print(f"Error: {e}")
                continue
            except Exception as e:
                print(
                    f"Unexpected error occurred while fetching page {i}, skipping..."
                )
                print(f"Error: {e}")
                continue

    def _generate_search_requests(self,
                                  search_keywords: List[str],
                                  entity_identifier: Optional[str],
                                  filing_category: Optional[str],
                                  exact_search: bool,
                                  start_date: date,
                                  end_date: date,
                                  wait_for_request_secs: int,
                                  stop_after_n: int) -> None:

        """
        Generates search requests for the given parameters and date range, recursiverly
        splitting the date range in two if the number of results is 10000 or more.
        :param search_keywords: Search keywords to input in the "Document word or phrase" field
        :param entity_identifier: Entity/Person name, ticker, or CIK number to input in the "Company name, ticker, or CIK" field
        :param filing_category: Filing category to select from the dropdown menu, defaults to None
        :param exact_search: Whether to perform an exact search on the search_keywords argument or not,
        defaults to False in order to return the maximum amount of search results by default
        :param start_date: Start date for the custom date range, defaults to 5 years ago to replicate the default behavior of the SEC website
        :param end_date: End date for the custom date range, defaults to current date in order to replicate the default behavior of the SEC website
        :param wait_for_request_secs: Number of seconds to wait for the request to complete, defaults to 8
        :param stop_after_n: Number of times to retry the request before failing, defaults to 3
        :return: None
        """

        # Fetch first page, verify that the request was successful by checking the result count value on the page
        request_args = self._generate_request_args(
            search_keywords=search_keywords,
            entity_identifier=entity_identifier,
            filing_category=filing_category,
            exact_search=exact_search,
            start_date=start_date,
            end_date=end_date,
            page_number=1,
        )
        url = f"{BASE_URL}{request_args}"

        # Try to fetch the first page and parse the number of results
        # In rare cases when the results are not empty, but the number of results cannot be parsed,
        # set num_results to 10000 in order to split the date range in two and continue
        try:
            num_results = self.fetch_first_page_results_number(url, wait_for_request_secs, stop_after_n)
        except ValueError as ve:
            print(f"Setting search results for range {start_date} -> {end_date} to 10000 due to error "
                  f"while parsing result number for seemingly non-empty results: {ve}")
            num_results = 10000

        # If we have 10000 results, split date range in two separate requests and fetch first page again, do so until
        # we have a set of date ranges for which none of the requests have 10000 results
        if num_results < 10000:
            print(f"Less than 10000 ({num_results}) results found for range {start_date} -> {end_date}, "
                  f"returning search request string...")
            self.search_requests.append(request_args)
        else:
            num_batches = min(((end_date - start_date).days, DEFAULT_BATCHES_NUMBER))
            print(
                f"10000 results or more for date range {start_date} -> {end_date}, splitting in {num_batches} intervals")
            dates = list(split_date_range_in_n(start_date, end_date, num_batches))
            for i, d in enumerate(dates):
                try:
                    start = d if i == 0 else d + timedelta(days=1)
                    end = dates[i + 1]
                    print(f"Trying to generate search requests for date range {start} -> {end} ...")
                    self._generate_search_requests(
                        search_keywords=search_keywords,
                        entity_identifier=entity_identifier,
                        filing_category=filing_category,
                        exact_search=exact_search,
                        start_date=start,
                        end_date=end,
                        wait_for_request_secs=wait_for_request_secs,
                        stop_after_n=stop_after_n,
                    )
                except IndexError:
                    pass

    def custom_text_search(
            self,
            search_keywords: List[str],
            entity_identifier: Optional[str],
            filing_category: Optional[str],
            exact_search: bool,
            start_date: date,
            end_date: date,
            wait_for_request_secs: int,
            stop_after_n: int,
    ) -> None:
        """
        Searches the SEC website for filings based on the given parameters, using Selenium for JavaScript support.

        :param search_keywords: Search keywords to input in the "Document word or phrase" field
        :param entity_identifier: Entity/Person name, ticker, or CIK number to input in the "Company name, ticker, or CIK" field
        :param filing_category: Filing category to select from the dropdown menu, defaults to None
        :param exact_search: Whether to perform an exact search on the search_keywords argument or not, defaults to False in order to return the maximum amount of search results by default
        :param start_date: Start date for the custom date range, defaults to 5 years ago to replicate the default behavior of the SEC website
        :param end_date: End date for the custom date range, defaults to current date in order to replicate the default behavior of the SEC website
        :param wait_for_request_secs: Number of seconds to wait for the request to complete, defaults to 10
        :param stop_after_n: Number of times to retry the request before failing, defaults to 3
        :return: None
        """

        self._generate_search_requests(
            search_keywords=search_keywords,
            entity_identifier=entity_identifier,
            filing_category=filing_category,
            exact_search=exact_search,
            start_date=start_date,
            end_date=end_date,
            wait_for_request_secs=wait_for_request_secs,
            stop_after_n=stop_after_n,
        )

        for r in self.search_requests:

            # Run generated search requests and paginate through results
            try:
                results: Iterator[Iterator[dict[str, Any]]] = self._fetch_search_request_results(
                    search_request=r,
                    wait_for_request_secs=wait_for_request_secs,
                    stop_after_n=stop_after_n,
                )
                self.write_results_to_csv(results, "results.csv")

            except Exception as e:
                print(f"Unexpected error occurred while fetching search request results for request parameters '{r}': {e}")
                print(f"Skipping...")

    @staticmethod
    def write_results_to_csv(data: Iterator[Iterator[Dict[str, Any]]], filename: str) -> None:
        """
        Writes the given generator of dictionaries to a CSV file. Assumes all dictionaries have the same keys,
        and that the keys are the column names. If file is already present, it appends the data to the file.

        Only writes the header once, and then writes the rows.

        :param data: Iterator of iterators of dictionaries to write to the CSV file
        :param filename: Name of the CSV file to write to
        :return: None
        """
        fieldnames = [
            "filing_type",
            "filed_at",
            "reporting_for",
            "entity_name",
            "company_cik",
            "place_of_business",
            "incorporated_location",
            "file_num",
            "film_num",
            "file_num_search_url",
            "filing_details_url",
            "filing_document_url"
        ]

        with open(filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if f.tell() == 0:
                writer.writeheader()
            for results_list_iterators in data:
                for r in results_list_iterators:
                    writer.writerow(r)
        print(f"Successfully wrote data to {filename}.")

    def fetch_first_page_results_number(self, url: str, wait_for_request_secs: int, stop_after_n: int) -> int:
        """
        Fetches the first page of results for the given URL and returns the number of results.

        :param url: URL to fetch the first page of results from
        :param wait_for_request_secs: number of seconds to wait for the request to complete
        :param stop_after_n: stop after n retries
        :return: number of results
        """

        # If we cannot fetch the first page after retries, abort
        try:
            fetch_page(self.driver, url, wait_for_request_secs, stop_after_n)(
                lambda: self.driver.find_element(By.XPATH, RESULTS_TABLE_SELECTOR).text.strip() != ""
            )
        except PageCheckFailedError:
            print(f"No results found for first page at URL {url}, aborting...")
            print(f"Please verify that the search/wait/retry parameters are correct and try again.")
            print(f"We recommend disabling headless mode for debugging purposes.")
            raise

        # If we cannot get number of results after retries, abort
        try:
            num_results = self._parse_number_of_results()
            return num_results
        except Exception as e:
            print(f"Failed to parse number of results for URL {url}, aborting...")
            print(f"Error: {e}")
            raise
