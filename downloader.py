import enum
import json
import logging
import os
import secrets
import shutil
import string
import sys
from binascii import a2b_base64
from collections import OrderedDict
from time import sleep, time
from typing import Dict

import undetected_chromedriver as uc
from PIL import Image
from dateutil.parser import parse
from selenium.common.exceptions import (
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait
from selenium_interceptor.interceptor import cdp_listener
from tqdm import tqdm

log = logging.getLogger()

BASE_URL = "https://www.fakku.net"
LOGIN_URL = f"{BASE_URL}/login/"
# Path to headless driver
if sys.platform == "win32":
    EXEC_PATH = "chromedriver.exe"
    BIN_PATH = "chrome.exe"
    USER_PATH = "_session"
    sp_c = "\\"
else:
    EXEC_PATH = "./chromedriver"
    BIN_PATH = "./chrome"
    USER_PATH = ".session"
    sp_c = "/"
# File with manga urls
URLS_FILE = "urls.txt"
# File with completed urls
DONE_FILE = "done.txt"
# File with prepared cookies
COOKIES_FILE = "cookies.json"  # easy to read and edit
# Root directory for manga downloader
ROOT_MANGA_DIR = "manga"
# Root directory for original files from server response
ROOT_RESPONSE_DIR = "response"
# Timeout to page loading in seconds
TIMEOUT = 10
# Wait between page loading in seconds
WAIT = 0.1
# User agent for web browser
USER_AGENT = None
# Should a cbz archive file be created
ZIP = False
# script version
script_version = "v0.2.2"

# create script tag to put in html body/head
js_name_todata = secrets.choice(string.ascii_letters) + "".join(
    secrets.choice(string.ascii_letters + string.digits) for _ in range(9)
)
js_script_in = """
<script>var s = document.createElement('script');
s.type = 'text/javascript';
var code = "HTMLCanvasElement.%s = HTMLCanvasElement.prototype.toDataURL;";
try {
      s.appendChild(document.createTextNode(code));
    } catch (e) {
      s.text = code;
}
s.onload = function() {
    this.remove();
};
(document.body || document.documentElement).appendChild(s);
(document.head || document.documentElement).appendChild(s);</script>
""" % (
    js_name_todata,
)


class ErrorReason(enum.Enum):
    """
    Network level fetch failure reason.
    """

    CONNECTION_ABORTED = "ConnectionAborted"

    def to_json(self):
        return self.value

    @classmethod
    def from_json(cls, fjson):
        return cls(fjson)


class CDPListenerMOD(cdp_listener):
    def __init__(self, driver):
        super().__init__(driver)
        self.mod_fakku_json = {}
        self.saved_requests = {}

    def start_threaded_mod(self, listener: Dict[str, callable]):
        if listener:
            self.listener = listener

        import threading

        thread = threading.Thread(target=self.trio_helper)
        self.thread = thread
        # change to daemon thread
        thread.daemon = True
        thread.start()

        while True:
            sleep(0.1)
            if self.is_running:
                break

        return thread

    async def requests_mod(self, connection):
        session, devtools = connection.session, connection.devtools
        pattern = self.specify_patterns(self.all_requests)
        await session.execute(devtools.fetch.enable(patterns=pattern))

        # buffer size increased from default 10 to 1000
        return session.listen(devtools.fetch.RequestPaused, buffer_size=1000)

    async def at_request(self, event, connection):
        session, devtools = connection.session, connection.devtools

        if "fakku.net" not in event.request.url or event.request.url.endswith(
            (".png", ".jpg", ".gif")
        ):
            log.debug(f"Aborted: {event.request.url}")
            return devtools.fetch.fail_request(
                request_id=event.request_id, error_reason=ErrorReason.CONNECTION_ABORTED
            )
        log.debug(f"Allowed: {event.request.url}")
        if event.response_status_code:
            if event.response_status_code == 200:
                log.debug(f"Response: {event.request.url}")

                if '/read' in event.request.url:
                    for header in event.response_headers:
                        if header.name == 'content-type' and header.value == 'application/json' :
                            body = await self.get_response_body(event.request_id)
                            decoded = self.decode_body(body[0], event)                            
                            self.mod_fakku_json = decoded

                if (
                    "fakku.net/hentai/" in event.request.url
                    and "/read/page/" in event.request.url
                ):
                    body = await self.get_response_body(event.request_id)
                    decoded = self.decode_body(body[0], event)

                    # modify response body
                    parsed_html = decoded
                    f = "<head>"
                    h_index = parsed_html.find(f) + len(f)
                    html2 = parsed_html[:h_index] + js_script_in + parsed_html[h_index:]
                    decoded = html2

                    log.debug("Response body modified")

                    encoded = self.encode_body(decoded)
                    body = (encoded, body[1])

                    return devtools.fetch.fulfill_request(
                        request_id=event.request_id,
                        response_code=event.response_status_code,
                        body=body[0],
                        response_headers=event.response_headers,
                    )

                if (
                    "books.fakku.net" in event.request.url
                    and "/images/" not in event.request.url
                ) and not self.mod_fakku_json:
                    body = await self.get_response_body(event.request_id)

                    decoded = self.decode_body(body[0], event)
                    self.mod_fakku_json = decoded

                if "b.fakku.net/" in event.request.url:
                    if event.request.url not in self.saved_requests:
                        body = await self.get_response_body(event.request_id)

                        headers = {}
                        for header in event.response_headers:
                            headers[header.name] = header.value

                        self.saved_requests[event.request.url] = {
                            "headers": headers,
                            "body": body[0],
                        }

                return devtools.fetch.continue_response(request_id=event.request_id)

        elif event.response_error_reason:
            log.debug(f"{event.response_error_reason}: {event.request.url}")

        return devtools.fetch.continue_request(
            request_id=event.request_id, intercept_response=True
        )


def append_images(
    imgs,
    direction="horizontal",
    bg_color=(255, 255, 255),
    alignment="center",
    src_type="unscrambled",
    dirc=None,
):
    """
    Appends images in horizontal/vertical direction. Used for joining spreads.

    Args:
        imgs: List of PIL images
        direction: direction of concatenation, 'horizontal' or 'vertical'
        bg_color: Background color (default: white)
        alignment: alignment mode if images need padding;
           'left', 'right', 'top', 'bottom', or 'center'
        src_type: image type, scrambled or not (default: 'unscrambled')
        dirc: reading direction, "Left to Right" or none (default: None)

    Returns:
        Concatenated image as a new PIL image object.
    """
    log.debug("Joining spreads")
    if dirc != "Left to Right":
        imgs.reverse()
    if type(imgs[0]) is str:
        images = map(Image.open, imgs)
    else:
        images = imgs

    widths, heights = zip(*(i.size for i in images))

    if direction == "horizontal":
        new_width = sum(widths)
        new_height = max(heights)
    else:
        new_width = max(widths)
        new_height = sum(heights)

    if type(imgs[0]) is str:
        images = list(map(Image.open, imgs))
    else:
        images = imgs

    if src_type == "scrambled":
        new_im = Image.new("RGBA", (new_width, new_height), color=bg_color)
    else:
        if images[0].mode == "L" and images[1].mode == "L":
            new_im = Image.new("L", (new_width, new_height), color=255)
        else:
            new_im = Image.new("RGB", (new_width, new_height), color=bg_color)

    offset = 0
    for im in images:
        if direction == "horizontal":
            y = 0
            if alignment == "center":
                y = int((new_height - im.size[1]) / 2)
            elif alignment == "bottom":
                y = new_height - im.size[1]
            new_im.paste(im, (offset, y))
            offset += im.size[0]
        else:
            x = 0
            if alignment == "center":
                x = int((new_width - im.size[0]) / 2)
            elif alignment == "right":
                x = new_width - im.size[0]
            new_im.paste(im, (x, offset))
            offset += im.size[1]

    return new_im


def fix_filename(filename):
    """
    Removes illegal characters from filename.
    """
    log.debug("Fixing string")
    filename = filename.replace("\n", "")
    filename = filename.replace("\r", "")
    filename = filename.replace("\t", "")
    filename = filename.replace("/", "⁄")
    if sys.platform == "win32":
        filename = filename.replace("?", "？")
        filename = filename.replace("\\", "⧹")
        filename = filename.replace(":", "꞉")
        filename = filename.replace("*", "＊")
        filename = filename.replace('"', "＂")
        filename = filename.replace("<", "＜")
        filename = filename.replace(">", "＞")
        filename = filename.replace("|", "｜")
    return filename


def _make_cbzfile(
    base_name, base_dir, verbose=0, dry_run=0, logger=None, owner=None, group=None
):
    """Create a cbz file from all the files under 'base_dir'.

    The output cbz file will be named 'base_name' + ".cbz".  Returns the
    name of the output cbz file.
    """
    import zipfile  # late import for breaking circular dependency

    cbz_filename = base_name + ".cbz"
    archive_dir = os.path.dirname(base_name)

    if archive_dir and not os.path.exists(archive_dir):
        if logger is not None:
            logger.info("creating %s", archive_dir)
        if not dry_run:
            os.makedirs(archive_dir)

    if logger is not None:
        logger.info("creating '%s' and adding '%s' to it", cbz_filename, base_dir)

    if not dry_run:
        with zipfile.ZipFile(cbz_filename, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            path = os.path.normpath(base_dir)
            if path != os.curdir:
                zf.write(path, path)
                if logger is not None:
                    logger.info("adding '%s'", path)
            for dirpath, dirnames, filenames in os.walk(base_dir):
                for name in sorted(dirnames):
                    path = os.path.normpath(os.path.join(dirpath, name))
                    zf.write(path, path)
                    if logger is not None:
                        logger.info("adding '%s'", path)
                for name in filenames:
                    path = os.path.normpath(os.path.join(dirpath, name))
                    if os.path.isfile(path):
                        zf.write(path, path)
                        if logger is not None:
                            logger.info("adding '%s'", path)

    return cbz_filename


shutil.register_archive_format("cbz", _make_cbzfile, [], "CBZ file")


def get_urls_list(urls_file, done_file):
    """
    Get list of urls from .txt file
    --------------------------
    param: urls_file -- string
        Name or path of .txt file with manga urls
    param: done_file -- string
        Name or path of .txt file with successfully downloaded manga urls
    return: urls -- list
        Urls from urls_file
    """
    log.debug("Parsing list of urls")
    done = set()
    with open(done_file, "r") as donef:
        for line in donef:
            done.add(line.replace("\n", ""))
    log.debug(f"Done: {len(done)}")

    urls = []
    with open(urls_file, "r") as f:
        for line in f:
            clean_line = line.replace("\n", "")
            if "fakku.net/hentai/" not in clean_line:
                continue
            if clean_line.startswith("#"):
                continue
            elif "#" in clean_line:
                clean_line = clean_line.split("#")[0]
            if clean_line not in done and clean_line not in urls:
                urls.append(clean_line)
    log.debug(f"Urls: {len(urls)}")
    if len(urls) == 0:
        log.info("Nothing to rip")
        exit()
    return urls


class JewcobDownloader:
    """
    Class which allows download manga.
    The main idea of download - using headless browser and saving both
    original images from fakku server responses and images rendered on html canvas
    with simple javascript injection and .toDataURL method
    ignoring shitty low quality screenshots
    ignoring bloated jpgs pretending to be png
    (original server jpg with quality 80-90 is better than bloated png created from that jpg)
    """

    def __init__(
        self,
        urls_file=URLS_FILE,
        done_file=DONE_FILE,
        cookies_file=COOKIES_FILE,
        root_manga_dir=ROOT_MANGA_DIR,
        root_response_dir=ROOT_RESPONSE_DIR,
        driver_path=EXEC_PATH,
        chrome_path=BIN_PATH,
        session_path=USER_PATH,
        timeout=TIMEOUT,
        wait=WAIT,
        login=None,
        password=None,
        _zip=ZIP,
        save_metadata=True,
        proxy=None,
        response=False,
        session=None,
        version=None,
    ):
        """
        param: urls_file -- string name of .txt file with urls
            Contains list of manga urls, that's to be downloaded
        param: done_file -- string name of .txt file with urls
            Contains list of manga urls that have successfully been downloaded
        param: cookies_file -- string name of .pickle file with cookies
            Contains binary data with cookies
        param: chrome_path -- string
            Path to the browser
        param: timeout -- float
            Timeout upon waiting for first page to load
        param: wait -- float
            Wait in seconds between pages downloading.
        param: login -- string
            Login or email for authentication
        param: password -- string
            Password for authentication
        """
        self.keep_response = response
        self.urls_file = urls_file
        self.urls = get_urls_list(urls_file, done_file)
        self.done_file = done_file
        self.cookies_file = cookies_file
        self.root_manga_dir = root_manga_dir
        self.root_response_dir = root_response_dir
        self.driver_path = driver_path
        self.chrome_path = chrome_path
        self.session_path = session_path
        self.browser = None
        self.timeout = timeout
        self.wait = wait
        self.login = login
        self.password = password
        self.zip = _zip
        self.save_metadata = save_metadata
        self.type = "unscrambled"
        self.proxy = proxy
        self.version = version
        self.session = session
        self.done = 0
        self.cdp_listener = None
        self.thread = None

    def init_browser(self, auth=False, gui=False):
        """
        Initializing browser and authenticate if necessary
        Obfuscation with undetected-chromedriver
        ---------------------
        param: auth -- bool
            If True: launch browser with GUI (for first time authentication)
            If False: skip authentication
        param: headless -- bool
            If True: launch browser in headless mode
            If False: launch browser with GUI
        """
        log.info("Initializing browser")
        if auth:
            if not os.path.exists(self.session_path):
                self.__auth()
            else:
                log.info("Using existing user data directory")
                self.session = True
        if os.path.exists(self.chrome_path):
            browser_executable = self.chrome_path
        else:
            browser_executable = None
        if os.path.exists(self.driver_path):
            driver_executable = self.driver_path
        else:
            driver_executable = None
        if self.session:
            user_data_dir = self.session_path
            log.info("Disabling headless")
            gui = True
        else:
            user_data_dir = None
        if self.version:
            version_main = self.version
        else:
            version_main = None

        options = uc.ChromeOptions()
        # avoid cors and other bullshit
        options.add_argument("--disable-web-security")
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")

        log.info("Connecting to chromedriver")
        try:
            self.browser = uc.Chrome(
                browser_executable_path=browser_executable,
                driver_executable_path=driver_executable,
                user_data_dir=user_data_dir,
                headless=not gui,
                options=options,
                patcher_force_close=True,
                enable_cdp_events=True,
                version_main=version_main,
            )
        except WebDriverException as err:
            log.debug(err.msg)
            cbvi = "Current browser version is "
            cdosv = "ChromeDriver only supports Chrome version "
            if cdosv in err.msg:
                log.info("Using older version of chromedriver")
                cbv = int(str(err.msg).split(cbvi)[1].split(".")[0])
                cdv = int(str(err.msg).split(cdosv)[1].split(" ")[0])
                if cbv > cdv:
                    self.version = cdv
                    return self.init_browser(auth, gui)
            else:
                log.info(
                    "Can't connect to driver. Remove session directory and try again"
                )
                exit()

        self.browser.set_script_timeout(self.timeout)
        self.browser.set_page_load_timeout(self.timeout)

        self.__set_cookies()

        log.debug("Checking if user is logged")
        try:
            if self.browser.current_url != BASE_URL:
                self.browser.get(BASE_URL)
            caret = self.browser.find_element(
                By.CSS_SELECTOR,
                "i.fa-angle-down",
            )
            login_check = caret.find_element(
                By.XPATH,
                "..",
            )
            cn = login_check.get_property("textContent")
            if not "My Account" in cn:
                log.debug(caret.get_attribute("outerHTML"))
                log.debug(login_check.get_attribute("outerHTML"))
                log.debug(cn)
                raise NoSuchElementException(msg="Missing My Account menu")
        except NoSuchElementException as err:
            log.info("You aren't logged in")
            log.info("Probably expired cookies")
            log.info("Remove cookies.json and try again")
            log.debug(err)
            self.browser.quit()
            exit()

        log.info("Browser initialized")

        self.cdp_listener = CDPListenerMOD(driver=self.browser)
        self.thread = self.cdp_listener.start_threaded_mod(
            listener={
                "listener": self.cdp_listener.requests_mod,
                "at_event": self.cdp_listener.at_request,
            }
        )

    def __set_cookies(self):
        """
        Changes local storage reader options and loads cookies from json file
        """
        log.debug("Loading cookies")
        self.waiting_loading_page(LOGIN_URL, page="login")
        # set fakku local storage options
        # UI Control Direction for Right to Left Content: Right to Left
        # Read in Either Direction on First Page: unchecked
        # Page Display Mode: Singles Pages Only
        # Page Scaling: Original Size
        # Fit to Width if Overwidth: unchecked
        # Background Color: Gray
        # But Not When Viewing Two Pages: unchecked
        self.browser.execute_script(
            "window.localStorage.setItem('fakku-uiControlDirection','rtl');"
            "window.localStorage.setItem('fakku-uiFirstPageControlDirectionFlip','false');"
            "window.localStorage.setItem('fakku-twoPageMode','0');"
            "window.localStorage.setItem('fakku-pageScalingMode','none');"
            "window.localStorage.setItem('fakku-fitIfOverWidth','false');"
            "window.localStorage.setItem('fakku-backgroundColor','#7F7B7B');"
            "window.localStorage.setItem('fakku-suppressWidthFitForSpreads','false');"
        )
        time_now = time()
        with open(self.cookies_file, "rb") as f:
            cookies = json.load(f)
            for cookie in cookies:
                if "expiry" in cookie:
                    cookie["expiry"] = int(cookie["expiry"])
                if cookie["name"] in {"fakku_sid", "fakku_zid"}:
                    if cookie["expiry"] < time_now:
                        log.info("Expired cookies")
                        log.info("Remove cookies.json and try again")
                        self.program_exit()
                self.browser.add_cookie(cookie)

    def __auth(self):
        """
        Authentication in browser with GUI for saving cookies in first time
        """
        log.debug("Authentication")

        if os.path.exists(self.chrome_path):
            browser_executable = self.chrome_path
        else:
            browser_executable = None
        if os.path.exists(self.driver_path):
            driver_executable = self.driver_path
        else:
            driver_executable = None
        if self.session:
            user_data_dir = self.session_path
        else:
            user_data_dir = None
        if self.version:
            version_main = self.version
        else:
            version_main = None

        options = uc.ChromeOptions()
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")


        log.info("Connecting to chromedriver")
        try:
            self.browser = uc.Chrome(
                browser_executable_path=browser_executable,
                driver_executable_path=driver_executable,
                user_data_dir=user_data_dir,
                headless=False,
                options=options,
                patcher_force_close=True,
                version_main=version_main,
            )
        except WebDriverException as err:
            log.debug(err.msg)
            cbvi = "Current browser version is "
            cdosv = "ChromeDriver only supports Chrome version "
            if cdosv in err.msg:
                log.info("Using older version of chromedriver")
                cbv = int(str(err.msg).split(cbvi)[1].split(".")[0])
                cdv = int(str(err.msg).split(cdosv)[1].split(" ")[0])
                if cbv > cdv:
                    self.version = cdv
                    return self.__auth()
                else:
                    raise err
            else:
                raise err

        self.browser.get(LOGIN_URL)
        try:
            h2 = self.browser.find_element(By.CSS_SELECTOR, "h2")
            h2 = h2.get_property("textContent")
            if "Checking if the site connection is secure" in h2:
                if self.session:
                    input("\nPress Enter to continue after you solved the captcha...")
                else:
                    log.info("Captcha detected using user data directory")
                    self.browser.quit()
                    self.session = True
                    return self.__auth()
            elif "While you wait visit our Discord" in h2:
                log.info("Your IP address is banned. Change it and try again")
                self.browser.quit()
                exit()
        except NoSuchElementException:
            pass

        if self.login is not None:
            self.browser.find_element(By.ID, "username").send_keys(self.login)
        if self.password is not None:
            self.browser.find_element(By.ID, "password").send_keys(self.password)
        self.browser.find_element(By.CSS_SELECTOR, 'button[class*="js-submit"]').click()

        input("\nPress Enter to continue after you login...")
        with open(self.cookies_file, "w") as f:
            json.dump(self.browser.get_cookies(), f, indent=True)

        self.browser.quit()

    def program_exit(self):
        log.info("Program exit.")
        self.browser.quit()
        self.cdp_listener.terminate_all()
        exit()

    def get_response_images(self, page, save_path, zpad):
        """
        Saves original images sent by fakku server, scrambled and unscrambled
        """
        if "response_path" not in self.cdp_listener.mod_fakku_json["pages"][page]:
            num = self.cdp_listener.mod_fakku_json["pages"][page]["page"]
            resp_url = self.cdp_listener.mod_fakku_json["pages"][page]["image"]
            image_path = None
            log.debug("Get response images")
            while not image_path:
                if resp_url in self.cdp_listener.saved_requests:
                    req_resp = self.cdp_listener.saved_requests[resp_url]

                    # FIXME: Do it right
                    resp_file_type = "png"
                    resp_data = a2b_base64(req_resp["body"])
                    resp_destination_file = os.sep.join(
                        [
                            save_path,
                            f"{num:0{zpad}d}.{resp_file_type}",
                        ]
                    )
                    with open(resp_destination_file, "wb") as file:
                        file.write(resp_data)
                    os.utime(resp_destination_file)
                    image_path = resp_destination_file
                sleep(self.wait)
            self.cdp_listener.mod_fakku_json["pages"][page][
                "response_path"
            ] = image_path

    def get_page_metadata(self, url):
        """crawl main page looking for metadata"""
        metadata = OrderedDict()
        if self.save_metadata != "basic":
            log.debug("Parsing right side for metadata")
            try:
                meta0 = self.browser.find_element(
                    By.CSS_SELECTOR,
                    'div[class^="block md:table-cell relative w-full align-top"]',
                )
                meta_rows = meta0.find_elements(
                    By.CSS_SELECTOR, 'div[class^="table text-sm w-full"]'
                )
                log.debug("Parsing right side rows")
                for meta_row in meta_rows:
                    try:
                        meta_row_left = meta_row.find_element(
                            By.CSS_SELECTOR,
                            'div[class^="inline-block w-24 text-left align-top"]',
                        )
                        left_text = meta_row_left.text
                    except NoSuchElementException as err:
                        log.debug(err.msg)
                        continue

                    if left_text in [
                        "Artist",
                        "Parody",
                        "Publisher",
                        "Language",
                        "Pages",
                        "Direction",
                    ]:
                        continue

                    log.debug(f"Parsing {left_text}")
                    meta_row_right = meta_row.find_element(
                        By.CSS_SELECTOR,
                        'div[class^="table-cell w-full align-top text-left"]',
                    )
                    a_tags = meta_row_right.find_elements(By.CSS_SELECTOR, "a")
                    if a_tags:
                        values = []
                        for a in a_tags:
                            if a.text == "+":
                                continue
                            values.append(a.text)
                        metadata[left_text] = values
                    else:
                        if left_text in {"Favorites"}:
                            metadata[left_text] = int(
                                "".join(meta_row_right.text.split(" ")[0].split(","))
                            )
                        else:
                            metadata[left_text] = meta_row_right.text
            except Exception as meta_err:
                log.info(f"Metadata parser issue right side, please report url: {url}")
                log.info(str(meta_err))

            log.debug("Parsing left side")
            try:
                meta1 = self.browser.find_element(
                    By.CSS_SELECTOR,
                    'div[class^="block sm:inline-block relative w-full align-top"]',
                )
                price_container = meta1.find_element(
                    By.CSS_SELECTOR,
                    'div[class^="rounded cursor-pointer right"]',
                )
                try:
                    price_left = price_container.find_element(
                        By.CSS_SELECTOR,
                        'div[class^="table w-auto text-right opacity-90 hover:opacity-100 js-purchase-product"]',
                    )
                    price = price_left.find_element(By.CSS_SELECTOR, "div").text
                    price = float(price[1:])
                    log.debug(price)
                    metadata["Price"] = price
                except NoSuchElementException as err:
                    log.debug(err.msg)
            except Exception as meta_err:
                log.info(f"Metadata parser issue left side, please report url: {url}")
                log.info(str(meta_err))

            log.debug("Parsing bottom")
            try:
                meta2d = self.browser.find_elements(By.CSS_SELECTOR, "div")
                for meta_rest in meta2d:
                    meta_id = meta_rest.get_attribute("id")
                    if "/related" in meta_id:
                        log.debug("Parsing Related")
                        div_book_titles = meta_rest.find_elements(
                            By.CSS_SELECTOR,
                            'div[class^="overflow-hidden relative rounded shadow-lg"]',
                        )
                        values = []
                        for dbt in div_book_titles:
                            a = dbt.find_element(By.CSS_SELECTOR, "a")
                            ah = a.get_attribute("href")
                            if ah not in values:
                                values.append(ah)
                        if len(values) == 1:
                            values = values[0]
                        metadata["Related"] = values
                    elif "/collections" in meta_id:
                        log.debug("Parsing Collections")
                        cols = meta_rest.find_elements(By.CSS_SELECTOR, "em")
                        if len(cols) > 0:
                            cols_meta = []
                            for col in cols:
                                cola = col.find_element(By.CSS_SELECTOR, "a")
                                colu = cola.get_attribute("href")
                                colt = cola.get_property("textContent")
                                cols_meta.append((colt, colu))
                            if len(cols_meta) > 0:
                                metadata["Collections"] = cols_meta
                    elif "/chapters" in meta_id:
                        log.debug("Parsing Chapters")
                        div_chapters = meta_rest.find_elements(
                            By.CSS_SELECTOR,
                            'div[class^="table relative w-full bg-white py-2 px-4 rounded dark:bg-gray-900"]',
                        )
                        cd = OrderedDict()
                        for dc in div_chapters:
                            dcn = dc.find_element(
                                By.CSS_SELECTOR,
                                'div[class^="inline-block pr-2 text-right w-8 align-top text-sm"]',
                            )
                            cn = int(dcn.get_property("innerHTML"))
                            dct = dc.find_element(
                                By.CSS_SELECTOR,
                                'div[class^="table-cell w-full align-top text-left text-sm"]',
                            )
                            a = dct.find_element(By.CSS_SELECTOR, "a")
                            ah = a.get_attribute("href")
                            cd[cn] = ah
                        metadata["Chapters"] = cd
            except Exception as meta_err:
                log.info(f"Metadata parser issue bottom, please report url: {url}")
                log.info(str(meta_err))

            log.debug(metadata)
        return metadata

    def get_api_metadata(self, metadata):
        """parse api json response for metadata"""
        metadata_api = OrderedDict()
        log.debug("Parsing api response for metadata")

        metadata_api["URL"] = self.cdp_listener.mod_fakku_json["content"]["content_url"]
        metadata_api["Title"] = self.cdp_listener.mod_fakku_json["content"][
            "content_name"
        ]

        content_artists = []
        for a in self.cdp_listener.mod_fakku_json["content"]["content_artists"]:
            content_artists.append(a["attribute"])
        metadata_api["Artist"] = content_artists

        content_series = []
        for s in self.cdp_listener.mod_fakku_json["content"]["content_series"]:
            content_series.append(s["attribute"])
        metadata_api["Parody"] = content_series

        if "content_publishers" in self.cdp_listener.mod_fakku_json["content"]:
            content_publishers = []
            for p in self.cdp_listener.mod_fakku_json["content"]["content_publishers"]:
                content_publishers.append(p["attribute"])
            metadata_api["Publisher"] = content_publishers

        metadata_api["Language"] = self.cdp_listener.mod_fakku_json["content"][
            "content_language"
        ]
        metadata_api["Pages"] = self.cdp_listener.mod_fakku_json["content"][
            "content_pages"
        ]

        content_description = self.cdp_listener.mod_fakku_json["content"][
            "content_description"
        ]
        metadata_api["Description"] = content_description
        if "content_direction" in self.cdp_listener.mod_fakku_json["content"]:
            metadata_api["Direction"] = self.cdp_listener.mod_fakku_json["content"][
                "content_direction"
            ]

        content_tags = []
        for t in self.cdp_listener.mod_fakku_json["content"]["content_tags"]:
            content_tags.append(t["attribute"])
        metadata_api["Tags"] = content_tags

        tp = list(self.cdp_listener.mod_fakku_json["pages"].keys())[0]
        metadata_api["Thumb"] = self.cdp_listener.mod_fakku_json["pages"][tp]["thumb"]

        log.debug(metadata_api)

        if "key_data" in self.cdp_listener.mod_fakku_json:
            self.type = "scrambled"

        if "Artist" in metadata_api:
            artist = metadata_api["Artist"]
            for i, v in enumerate(artist):
                artist[i] = fix_filename(v)
            if len(artist) > 2:
                artist = "Various"
            elif len(artist) == 2:
                artist = ", ".join(artist)
            elif len(artist) == 1:
                artist = artist[0]
            else:
                artist = None
        else:
            artist = None
        log.debug(artist)

        if "Title" in metadata_api:
            title = metadata_api["Title"]
            title = fix_filename(title)
        else:
            title = None
        log.debug(title)

        if "Circle" in metadata:
            circle = metadata["Circle"]
            for i, v in enumerate(circle):
                circle[i] = fix_filename(v)
            if len(circle) > 2:
                circle = "Various"
            elif len(circle) == 2:
                circle = ", ".join(circle)
            elif len(circle) == 1:
                circle = circle[0]
            else:
                circle = None
        else:
            circle = None
        log.debug(circle)

        if "Magazine" in metadata:
            extra = metadata["Magazine"]
            # remove New Illustration from name because it's not a magazine
            if "New Illustration" in extra:
                extra.remove("New Illustration")
            for i, v in enumerate(extra):
                extra[i] = fix_filename(v)
            if len(extra) > 2:
                extra = "Various"
            elif len(extra) == 2:
                extra = ", ".join(extra)
            elif len(extra) == 1:
                extra = extra[0]
            else:
                extra = None
        else:
            extra = None
        if extra:
            extra = f" ({extra})"
        else:
            extra = " (FAKKU)"
        log.debug(extra)

        if "Direction" in metadata_api:
            direction = metadata_api["Direction"]
        else:
            direction = "Right to Left"
        log.debug(direction)

        if artist:
            if circle:
                folder_title = "[" + circle + " (" + artist + ")" + "] " + title + extra
            else:
                folder_title = "[" + artist + "] " + title + extra
        elif circle:
            folder_title = "[" + circle + "] " + title + extra
        else:
            folder_title = title + extra
        manga_folder = os.sep.join([self.root_manga_dir, folder_title])
        if not os.path.exists(manga_folder):
            os.mkdir(manga_folder)
        log.debug(manga_folder)
        response_folder = os.sep.join([self.root_response_dir, folder_title])
        if not os.path.exists(response_folder):
            os.mkdir(response_folder)
        return metadata_api, manga_folder, response_folder, direction

    def load_all(self):
        """
        load main page
        load first reader page
        click the rest
        dumps images data urls from html canvas as .png
        """
        log.debug("Starting main downloader function")
        if not os.path.exists(self.root_manga_dir):
            os.mkdir(self.root_manga_dir)
        if not os.path.exists(self.root_response_dir):
            os.mkdir(self.root_response_dir)

        urls_processed = 0
        for url in self.urls:
            log.info(url)
            self.timeout = TIMEOUT
            self.wait = WAIT
            self.cdp_listener.mod_fakku_json = {}
            self.type = "unscrambled"
            self.cdp_listener.saved_requests = {}
            self.done = 0

            self.waiting_loading_page(url, page="main")

            log.debug("Checking if gallery is available, green button")
            try:
                bt = self.browser.find_element(
                    By.CSS_SELECTOR, 'a[class^="button-green"]'
                )
                if "Start Reading" not in bt.text:
                    log.info(f"{bt.text}: {url}")
                    urls_processed += 1
                    continue
            except NoSuchElementException as err:
                log.info(f"No green button: {url}")
                log.debug(err.msg)
                urls_processed += 1
                continue

            metadata = self.get_page_metadata(url)

            page_count = 2
            log.debug(page_count)

            folder_title = url.split("/")
            if url.endswith("/"):
                folder_title = folder_title[-2]
            else:
                folder_title = folder_title[-1]

            log.info(f'Downloading "{folder_title}" manga.')

            log.debug("First page, testing injection")
            js_test = False
            while not js_test:
                self.waiting_loading_page(f"{url}/read/page/1", page="first")
                js_script_test = """
                var dataURL = HTMLCanvasElement.%s;
                return dataURL;
                """ % (
                    js_name_todata,
                )
                try:
                    jt = self.browser.execute_script(js_script_test)
                    # jt result should be empty dict {}
                    if type(jt) is dict:
                        js_test = True
                    else:
                        js_test = False
                        log.info("retry")
                except JavascriptException:
                    pass
                sleep(self.wait)

            log.debug("Waiting for api response")
            while "content" not in self.cdp_listener.mod_fakku_json:
                sleep(self.wait)

            log.debug(self.cdp_listener.mod_fakku_json["content"].keys())

            (
                metadata_api,
                manga_folder,
                response_folder,
                direction,
            ) = self.get_api_metadata(metadata)

            for k, v in metadata_api.items():
                metadata[k] = v
            log.debug(metadata)

            page_count = int(metadata["Pages"])
            if page_count > 1000:
                padd = 4
            elif page_count > 100:
                padd = 3
            elif page_count > 10:
                padd = 2
            else:
                padd = 2

            spreads = dict()
            for spread in self.cdp_listener.mod_fakku_json["spreads"]:
                left = str(spread[0])
                right = str(spread[-1])
                if left == right:
                    continue
                else:
                    spreads[right] = (left, right)

            progress_bar = tqdm(
                total=page_count, desc="Working...", leave=False, position=0
            )

            for page in self.cdp_listener.mod_fakku_json["pages"]:
                # get page response image
                self.get_response_images(page, response_folder, padd)

                # wait until loader hides itself
                WebDriverWait(self.browser, self.timeout).until(
                    expected_conditions.invisibility_of_element_located(
                        (By.CLASS_NAME, "loader")
                    )
                )

                # wait until read notification hides
                WebDriverWait(self.browser, self.timeout).until(
                    expected_conditions.invisibility_of_element_located(
                        (By.CSS_SELECTOR, 'div[class^="ui notify-container large"]')
                    )
                )

                page_num = self.cdp_listener.mod_fakku_json["pages"][page]["page"]

                if self.type == "scrambled":
                    log.debug("Parsing PageView layer for canvas")
                    canvas_found = None
                    while not canvas_found:
                        try:
                            page_view = self.browser.find_element(
                                By.CSS_SELECTOR, 'div.layer[data-name="PageView"]'
                            )
                            images_canvas = page_view.find_elements(
                                By.CSS_SELECTOR, "canvas"
                            )
                            if len(images_canvas) > 0:
                                canvas_found = images_canvas[-1]
                        except StaleElementReferenceException as err:
                            log.debug(err.msg)
                        sleep(self.wait)

                    log.debug("Get image from canvas")
                    destination_file = os.sep.join(
                        [manga_folder, f"{page_num:0{padd}d}.png"]
                    )
                    js_script = f"""
                    var dataURL = HTMLCanvasElement.%s.call(arguments[0], \"image/png\");
                    return dataURL;
                    """ % (
                        js_name_todata,
                    )

                    rendered_image_data_url = self.browser.execute_script(
                        js_script, canvas_found
                    )

                    response_data = a2b_base64(rendered_image_data_url.split(",")[1])

                    with open(destination_file, "wb") as f:
                        f.write(response_data)
                    rit = os.path.getmtime(
                        self.cdp_listener.mod_fakku_json["pages"][page]["response_path"]
                    )
                    os.utime(destination_file, (rit, rit))
                else:
                    log.debug("Copy image from server response")
                    resp_img = self.cdp_listener.mod_fakku_json["pages"][page][
                        "response_path"
                    ]
                    ext = resp_img.split(".")[-1]
                    destination_file = os.sep.join(
                        [manga_folder, f"{page_num:0{padd}d}.{ext}"]
                    )
                    shutil.copy2(resp_img, destination_file)
                self.cdp_listener.mod_fakku_json["pages"][page][
                    "image_path"
                ] = destination_file
                log.debug(destination_file)

                if page in spreads:
                    log.debug("Creating spread")
                    left = spreads[page][0]
                    right = spreads[page][-1]
                    fin_img = []
                    im_l = self.cdp_listener.mod_fakku_json["pages"][left]["image_path"]
                    fin_img.append(im_l)
                    im_r = self.cdp_listener.mod_fakku_json["pages"][right][
                        "image_path"
                    ]
                    fin_img.append(im_r)

                    nam_l = im_l.split(sp_c)[-1].split(".")[0]
                    ext_l = im_l.split(sp_c)[-1].split(".")[-1]
                    nam_r = im_r.split(sp_c)[-1].split(".")[0]
                    ext_r = im_r.split(sp_c)[-1].split(".")[-1]

                    spread_name = nam_l + "-" + nam_r
                    destination_file_spread = os.sep.join(
                        [manga_folder, f"{spread_name}a.png"]
                    )

                    combo = append_images(
                        fin_img,
                        direction="horizontal",
                        alignment="none",
                        src_type=self.type,
                        dirc=direction,
                    )
                    combo.save(destination_file_spread)
                    im_r_mt = os.path.getmtime(im_r)
                    os.utime(destination_file_spread, (im_r_mt, im_r_mt))

                    destination_file_l = os.sep.join(
                        [manga_folder, f"{nam_l}b.{ext_l}"]
                    )
                    destination_file_r = os.sep.join(
                        [manga_folder, f"{nam_r}c.{ext_r}"]
                    )

                    shutil.move(im_l, destination_file_l)
                    self.cdp_listener.mod_fakku_json["pages"][left][
                        "image_path"
                    ] = destination_file_l
                    shutil.move(im_r, destination_file_r)
                    self.cdp_listener.mod_fakku_json["pages"][right][
                        "image_path"
                    ] = destination_file_r

                    log.debug(destination_file_spread)

                progress_bar.update()

                if self.done < len(self.cdp_listener.mod_fakku_json["pages"]):
                    log.debug("Clicking next page")
                    ui = self.browser.find_element(
                        By.CSS_SELECTOR, 'div.layer[data-name="UI"]'
                    )
                    ui.click()
                    self.done += 1
                    sleep(self.wait)
            progress_bar.close()

            # delete old requests
            del self.cdp_listener.saved_requests

            if self.done > 0 and log.level == 10:
                resp_info_file = os.sep.join([response_folder, f"fakku_data.json"])
                cks = self.browser.get_cookies()
                for cookie in cks:
                    if cookie["name"] in {"fakku_zid"}:
                        self.cdp_listener.mod_fakku_json[cookie["name"]] = cookie[
                            "value"
                        ]

                json.dump(
                    self.cdp_listener.mod_fakku_json,
                    open(resp_info_file, "w", encoding="utf-8"),
                    indent=True,
                    ensure_ascii=False,
                )

            if self.save_metadata != "none":
                metd = OrderedDict()
                sorted_d = sorted(metadata.items(), key=lambda x: x[0])
                for sd in sorted_d:
                    sdd = sd[1]
                    if type(sdd) is list and len(sdd) == 1:
                        sdd = sd[1][0]
                    metd[sd[0]] = sdd

                log.debug("Dumping metadata in info.json file")
                json_info_file = os.sep.join(
                    [
                        manga_folder,
                        "info.json",
                    ]
                )

                with open(json_info_file, "w", encoding="utf-8") as f:
                    json.dump(metd, f, indent=True, ensure_ascii=False)

            if self.zip:
                log.debug("Creating a cbz and deleting the image folder after creation")
                archive_name = shutil.make_archive(folder_title, "cbz", manga_folder)
                shutil.move(archive_name, self.root_manga_dir)
                shutil.rmtree(manga_folder)

            if not self.keep_response:
                shutil.rmtree(response_folder)

            log.info(">> manga done!")
            with open(self.done_file, "a") as done_file_obj:
                done_file_obj.write(f"{url}\n")
            urls_processed += 1
            log.debug("Finished parsing page")
            sleep(self.wait)
        log.info(f"Urls processed: {urls_processed}")
        self.program_exit()

    def waiting_loading_page(self, url, page=None):
        """
        Awaiting while page will load
        ---------------------------
        param: page -- string
            login -- waiting for login page
            main -- waiting for main manga page
            first -- waiting for first reader page
        """
        log.debug(f"Loading url: {url}")
        tried = 0
        while True:
            try:
                self.browser.get(url)
                break
            except TimeoutException as err:
                log.info("Error: timed out waiting for page to load.")
                if tried > 3:
                    log.info(err.msg)
                    log.info(f"Connection timeout: {url}")
                    self.program_exit()
                tried += 1
                self.timeout *= 1.5
                self.browser.set_script_timeout(self.timeout)
                self.browser.set_page_load_timeout(self.timeout)
                sleep(self.wait)

        if page == "main":
            elem_xpath = "//div[contains(@class, 'inline-block relative text-center mx-auto')]"
        elif page == "first":
            elem_xpath = "//div[@data-name='PageView']"
        else:
            elem_xpath = "//link[@rel='icon']"

        elm_found = False
        tried = 0
        log.debug("Waiting for element")
        while not elm_found:
            try:
                element = expected_conditions.presence_of_element_located(
                    (By.XPATH, elem_xpath)
                )
                elm_found = WebDriverWait(self.browser, self.timeout).until(element)
            except TimeoutException as err:
                try:
                    h2 = self.browser.find_element(By.CSS_SELECTOR, "h2")
                    h2 = h2.get_property("textContent")
                    if "While you wait visit our Discord" in h2:
                        log.info(
                            "FAKKU is temporarily down for maintenance or your IP address is banned."
                        )
                        self.program_exit()
                except NoSuchElementException as err2:
                    log.debug(err2.msg)
                log.info("Error: timed out waiting for element to load.")
                log.debug(err.msg)
            if tried > 3:
                log.info("Connection issues, Try again")
                self.program_exit()
            elif tried > 2:
                log.info("Some connection issues, refreshing page")
                self.timeout *= 1.5
                self.browser.set_script_timeout(self.timeout)
                self.browser.set_page_load_timeout(self.timeout)
                self.browser.refresh()
            else:
                log.debug("Sleeping")
                sleep(self.wait)
