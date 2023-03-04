import argparse
import logging
import sys
from pathlib import Path

from downloader import (
    COOKIES_FILE,
    DONE_FILE,
    JewcobDownloader,
    ROOT_MANGA_DIR,
    TIMEOUT,
    URLS_FILE,
    WAIT,
    script_version,
)


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        "-f",
        "--file_urls",
        type=str,
        default=URLS_FILE,
        help=f".txt file that contains list of urls for download \
            By default -- {URLS_FILE}",
    )
    argparser.add_argument(
        "-d",
        "--done_file",
        type=str,
        default=DONE_FILE,
        help=f".txt file that contains list of urls that have been downloaded. \
            This is used to resume in the event that the process stops midway. \
            By default -- {DONE_FILE}",
    )
    argparser.add_argument(
        "-c",
        "--cookies_file",
        type=str,
        default=COOKIES_FILE,
        help=f"Binary file that contains saved cookies for authentication. \
            By default -- {COOKIES_FILE}",
    )
    argparser.add_argument(
        "-o",
        "--output_dir",
        type=str,
        default=ROOT_MANGA_DIR,
        help=f"The directory that will be used as the root of the output \
            By default -- {ROOT_MANGA_DIR}",
    )
    argparser.add_argument(
        "-l",
        "--login",
        type=str,
        default=None,
        help="Login or email for authentication",
    )
    argparser.add_argument(
        "-p", "--password", type=str, default=None, help="Password for authentication"
    )
    argparser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=TIMEOUT,
        help=f"Timeout in seconds for script and page loading. \
            Increase when on slow connection like proxy. By default -- {TIMEOUT} sec",
    )
    argparser.add_argument(
        "-w",
        "--wait",
        type=float,
        default=WAIT,
        help=f"Wait time in seconds for pauses between downloading pages \
            Increase this argument if you become blocked. By default -- {WAIT} sec",
    )
    argparser.add_argument(
        "--nozip",
        dest="zip",
        action="store_false",
        help=f"By default this program creates a CBZ file containing the images as an output. \
                Setting this creates a folder instead.",
    )
    argparser.add_argument(
        "--GUI",
        dest="gui",
        action="store_true",
        help=f"Run with browser in graphic mode. By default this program runs in headless mode.",
    )
    argparser.add_argument(
        "--DEBUG",
        dest="debug",
        action="store_true",
        help=f"Run in debug mode, saves logs in debug.log file. Default false.",
    )
    argparser.add_argument(
        "--nometa",
        dest="metadata",
        action="store_false",
        help=f"By default this program keep gallery metadata in info.json file inside directory/archive. 3-4 parsers\
                Setting this disables metadata file creation.",
    )
    argparser.add_argument(
        "--basic_metadata",
        dest="basic_metadata",
        action="store_true",
        help=f"Store only basic info in metadata info.json file. \
         no parser, fast, -Magazine -Event -Circle -Price -Related -Chapters -Collections",
    )
    argparser.add_argument(
        "--proxy",
        dest="proxy",
        type=str,
        default=None,
        help="Use proxy server for connection. \
         example: --proxy socks5://user:pass@192.168.10.100:8888",
    )
    argparser.add_argument(
        "--response",
        dest="response",
        action="store_true",
        help="Keep response directory with scrambled images and fakku api response file",
    )
    args = argparser.parse_args()
    log_handlers = []
    if args.debug:
        log_level = logging.DEBUG
        log_formatter_steam = logging.Formatter(
            "%(levelname)s : %(name)s : %(module)s : %(funcName)s : %(lineno)d : %(message)s"
        )
        log_formatter_file = "%(asctime)s %(levelname)s %(name)s %(filename)s %(module)s %(funcName)s %(lineno)d %(message)s"
        log_file = "debug.log"
        log_handlers.append(logging.FileHandler(log_file, mode="w", encoding="utf-8"))
    else:
        log_level = logging.INFO
        log_formatter_steam = logging.Formatter("%(message)s")
        log_formatter_file = ""

    # added logging for both console and file
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_formatter_steam)
    stream_handler.setLevel(log_level)
    log_handlers.append(stream_handler)
    logging.basicConfig(
        level=log_level, format=log_formatter_file, handlers=log_handlers
    )

    log = logging.getLogger()
    if args.debug:
        log.debug(f"Version: %s", script_version)
        log.debug("Python %s - %s", sys.version, sys.platform)
        log.debug(sys.argv)
        log.debug(args)

    # ignore handlers other than root
    logging.getLogger("hpack").setLevel(logging.ERROR)
    logging.getLogger("PIL.PngImagePlugin").setLevel(logging.ERROR)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(
        logging.ERROR
    )
    logging.getLogger("trio-websocket").setLevel(logging.ERROR)
    logging.getLogger("trio_cdp").setLevel(logging.ERROR)
    logging.getLogger("undetected_chromedriver").setLevel(logging.ERROR)

    file_urls = Path(args.file_urls)
    if not file_urls.is_file() or file_urls.stat().st_size == 0:
        logging.info(
            f"File {args.file_urls} does not exist or empty.\n"
            + "Create it and write the list of manga urls first.\n"
        )
        exit()

    # Create empty done.text if it not exists
    if not Path(args.done_file).is_file():
        Path(args.done_file).touch()

    if args.basic_metadata:
        args.metadata = "basic"
    elif args.metadata:
        args.metadata = "standard"
    else:
        args.metadata = "none"

    loader = JewcobDownloader(
        urls_file=args.file_urls,
        done_file=args.done_file,
        cookies_file=args.cookies_file,
        root_manga_dir=args.output_dir,
        login=args.login,
        password=args.password,
        timeout=args.timeout,
        wait=args.wait,
        _zip=args.zip,
        save_metadata=args.metadata,
        proxy=args.proxy,
    )

    if not Path(args.cookies_file).is_file():
        logging.info(
            f"Cookies file({args.cookies_file}) are not detected. Please, "
            + "login in next step for generate cookie for next runs."
        )
        loader.init_browser(auth=True, gui=args.gui)
    else:
        logging.info(f"Using cookies file: {args.cookies_file}")
        loader.init_browser(gui=args.gui)

    loader.load_all()


if __name__ == "__main__":
    main()
