forked from https://github.com/Hikot0shi/fakku-downloader/

tested on 7595 urls   
7595 works downloaded without an issue (games and anime skipped)   
running 24x7 on loonix debian vps with a single ip with:   
```bash
while :; do python3 main.py --nozip --basic_metadata --DEBUG; sleep 100; done
```
I never got banned, throttled or blocked by mitm cuckflare   
default settings are sufficient   
you don't have to change any wait time or timeouts   

# jewcob-downloader

Jewcob-downloader - this is python script that allows downloading manga directly from fakku.net.

## The problem

> *Fakku.net manga reader has a good protection from download.* 

- server sends scrambled image - jpeg quality 80-90 for color, png for greyscale
- reader unscramble image and put it on html canvas - returns rgba values for each pixel
- reader overwrites javascript methods to block downloading images from canvas

## Solution

- screenshots of canvas/layer element
  - pros:
    - quick and easy `find_elements_by().screenshot()`
  - cons:
    - defaults to screenshots with the size of browser window
    - `browser.window` needs to be resized to canvas size
	- hard to tell when canvas resize ends (`window.addEventListener("resize")` or `canvas.onresize`)
    - ends up with hardcoded wait time after resize but before screenshot
- injecting javascript before evaluation of reader.min.js https://intoli.com/blog/javascript-injection/  
  - pros:
    - easy when using cdp, selenium `execute_cdp_cmd()` `Page.addScriptToEvaluateOnNewDocument`, puppeteer and pyppeteer `evaluateOnNewDocument()`
    - evaluates injected javascript code before external scripts
  - cons:
    - cors https://fetch.spec.whatwg.org/#http-cors-protocol https://www.w3.org/wiki/CORS_Enabled 
    - when implemented with webextension can't run headless chromium  
    - when implemented with html response body modification it requires request and response interceptor 
    - when implemented with cdp `evaluateOnNewDocument` prone to race conditions, often fails in selenium

## Quality

There is no difference in quality between `element.screenshot` and `canvas.todataurl`.  Both returns RGBA32 PNG.  
If both width and height of scrambled/obfuscated jpeg image are multiple of 8 (ex 1920x1360) you can get unscrambled/deobfuscated image using lossless jpeg transformation with [jpegtran](https://jpegclub.org/jpegtran)  
The rest are kept as bloated png with 4x or 5x the size of jpeg, with the same shitty jpeg quality.  
Basically fakku is serving shitty color jpegs and most rippers are treating them as high quality lossless pngs. 

## Implementation
- open /read/page/1
- wait for response with scrambled image
- wait fo .loader to hide
- wait fo notification message to hide
- take screenshot of canvas/layer element / get todataurl
- click() on the layer to load next image
- repeat for all images

## Changes from the fakku-downloader
- readable json cookies instead of pickle
- css selectors instead of bs
- canvas toDataURL instead of screenshots
- added js injections with selenium-interceptor and cdp
- added response image downloader with selenium-interceptor and cdp
- added support for spreads
- removed not working obfuscation, used undetected-chromedriver instead
- directory/archive and image naming schemes match rbot rips https://sukebei.nyaa.si/user/rbot2000

## How to launch  

### From source  
1) Download or clone this repository
2) Download and install [Python](https://www.python.org/downloads/release)  version >= 3.10
3) Create **urls.txt** file in root folder and write into that urls of manga one by line
4) Install all requirements <code>pip install -r requirements.txt</code>
5) Open root folder in command line and run the command <code>python main.py</code>

## Some features
* Use option -w for set wait time between loading the pages.
* Use option -t for set timeout for loading pages.
* Use option -l and -p for write the login and password from fakku.net
* More technical options you can find via --help

# Results

pyppeteer rewrite  
unstable, on every `page.goto()` there is 50% chance of it throwing `pyppeteer.errors.PageError: Page crashed!`, test took 33 sec

selenium using cdp  
unstable 50% chance of `addScriptToEvaluateOnNewDocument()` failing, slightly slower than pyppeteer, test took 37 sec

selenium with selenium-wire mitmproxy  
stable, worked through 68 free links without an issue, slightly slower than cdp, test took 43 sec

selenium using `CanvasRenderingContext2D.originalgetImageData()` method  
slow, very slow, ~20 seconds to get pixels from browser and to put them back together into single rgba32 image, ~20s per image

selenium using `canvas.toblob()` can't save files outside of browser download location

selenium using cdp (Chrome DevTools Protocol) Fetch Domain with Selenium-Interceptor  
stable, worked through 69 free links without an issue, slightly faster than selenium-wire, test took 42 sec  

# Extra: Enable controversial content

https://archived.moe/h/thread/6271290/#6289883  
https://archiveofsins.com/h/thread/6271290/#6289883  

> 1. Go to https://www.fakku.net/account/preferences
> 2. Open the browser console (F12)
> 3. Paste this into the console and press enter:
> ```jquery
> $("form.js-start-disabled-button").first().append($('<input type="hidden" name="content_controversial" value="1" />')).find(':submit').attr("disabled", false).click();
> ```
> 4. It won't return anything but if you check controversial gallery it should work now.
