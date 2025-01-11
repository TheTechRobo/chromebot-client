import asyncio, dataclasses, time, typing, urllib.request, json, shutil, os

import logging
logger = logging.getLogger("mnbot")
del logging

from meta import VERSION
from rethinkdb import r

if typing.TYPE_CHECKING:
    from tracker import Websocket

import brozzler
import requests

PROXY_URL = "warcprox:8000"

######### INITIALISATION #########

# Wait for warcprox to start
while True:
    try:
        with urllib.request.urlopen("http://" + PROXY_URL.rstrip("/") + "/status") as conn:
            if conn.getcode() == 200:
                break
            else:
                logger.info("warcprox isn't healthy yet, sleeping")
                time.sleep(5)
    except Exception:
        logger.info("no warcprox yet, sleeping")
        time.sleep(5)

# Convenience function to get the dedup DB
def _dedup_db():
    return r.db("cb-warcprox").table("dedup")

# Convenience function to get the stats DB
def _stats_db():
    return r.db("cb-warcprox").table("stats")

# Add our index
def _setup_db():
    conn = r.connect(host = "rethinkdb")
    indexes = _dedup_db().index_list().run(conn)
    #if "mnbot-bucket" not in indexes:
    #    _dedup_db().index_create("mnbot-bucket", lambda row : row['key'].split("|", 1)).run(conn)
    if "mnbot-date" not in indexes:
        _dedup_db().index_create("mnbot-date", r.iso8601(r.row['date'])).run(conn)
    conn.close()
_setup_db()
r.set_loop_type("asyncio")

######### THE ACTUALLY IMPORTANT STUFF #########

@dataclasses.dataclass
class Job:
    full_job: dict
    url: str
    warc_prefix: str
    dedup_bucket: str
    stats_bucket: str
    stealth_ua: bool
    custom_js: typing.Optional[str]
    cookie_jar: typing.Optional[bytes]

    mnbot_info_url: str

@dataclasses.dataclass
class Result:
    final_url: str
    outlinks: list
    custom_js_result: typing.Optional[dict]
    status_code: int

    def dict(self) -> dict[str, typing.Any]:
        return {
            "final_url": self.final_url,
            "outlinks": self.outlinks,
            "custom_js_result": self.custom_js_result
            # Don't include status code because that's already in the WARC
        }

def thumb_jpeg(full_jpeg):
    # This really should be a static method...
    return brozzler.BrozzlerWorker.thumb_jpeg(None, full_jpeg)

class Brozzler:
    def __init__(self, browsers: int):
        self.pool = brozzler.BrowserPool(
            browsers,
            chrome_exe = shutil.which("chromium"),
            ignore_cert_errors = True
        )

    def _write_warcprox_record(self, url: str, content_type: str, payload, warc_prefix):
        logger.debug(f"writing {url} ({content_type}) to WARC")
        headers = {
            "Content-Type": content_type,
            "WARC-Type": "resource",
            "Host": PROXY_URL,
            "Warcprox-Meta": json.dumps({"warc-prefix": warc_prefix})
        }
        request = urllib.request.Request(
            url,
            method = "WARCPROX_WRITE_RECORD",
            headers = headers,
            data = payload
        )
        request.type = "http"
        request.set_proxy(PROXY_URL, "http")

        with urllib.request.urlopen(request, timeout=600) as response:
            if response.getcode() != 204:
                raise RuntimeError("Bad status code from Warcprox")

    def _on_screenshot(self, screenshot, url: str, warc_prefix: str):
        # Inspired by Brozzler's implementation of this. Brozzler is also Apache2.0-licenced
        logger.debug("writing screenshot")
        thumbnail = thumb_jpeg(screenshot)
        self._write_warcprox_record(
            url = "screenshot:" + url,
            content_type = "image/jpeg",
            payload = screenshot,
            warc_prefix = warc_prefix
        )
        self._write_warcprox_record(
            url = "thumbnail:" + url,
            content_type = "image/jpeg",
            payload = thumbnail,
            warc_prefix = warc_prefix
        )

    def _run_cdp_command(self, browser: brozzler.Browser, method: str, params: dict = {}) -> typing.Any:
        # Abuse brozzler's innards a bit to run a custom CDP command.
        # If this breaks, I was never here.
        # TODO: Burn with fire.
        logger.debug(f"running CDP command {method}")
        assert browser.websock_thread
        browser.websock_thread.expect_result(browser._command_id.peek())
        msg_id = browser.send_to_chrome(
            method = method,
            params = params
        )
        logger.debug("waiting for response")
        browser._wait_for(
            lambda : browser.websock_thread.received_result(msg_id),
            timeout = 60
        )
        message = browser.websock_thread.pop_result(msg_id)
        m = repr(message)
        if len(m) < 1024:
            logger.debug(f"received response: {message}")
        else:
            logger.debug(f"received response (too long for logs)")
        return message

    def _proxy_url(self, url: str, headers: dict):
        proxies = {
            "http": f"http://{PROXY_URL}",
            "https": f"https://{PROXY_URL}"
        }
        logger.debug(f"fetching {url}")
        return requests.get(
            url,
            proxies = proxies,
            headers = headers,
            verify = False
        )

    def _best_effort_proxy_url(self, url: str, headers: dict):
        try:
            return self._proxy_url(url, headers)
        except Exception:
            logger.warning(f"failed to proxy URL; stifling issue", exc_info = True)
            return None

    def _brozzle(self, browser: brozzler.Browser, job: Job) -> Result:
        assert not browser.is_running()
        extra_headers = {
            "Warcprox-Meta": json.dumps({
                "warc-prefix": job.warc_prefix,
                #"dedup-buckets": {"successful_jobs": "ro", dedup_bucket: "rw"}
                "dedup-buckets": {job.dedup_bucket: "rw"},
                "stats": {"buckets": [job.stats_bucket]}
            }),
        }
        logger.debug("starting browser")
        browser.start(
            proxy = "http://" + PROXY_URL,
            cookie_db = job.cookie_jar
        )

        logger.debug("getting user agent")
        ua = self._run_cdp_command(
            browser,
            "Runtime.evaluate",
            {"expression": "navigator.userAgent", "returnByValue": True}
        )['result']['result']['value']
        logger.debug(f"got user agent {ua}")
        # pretend we're not headless
        ua = ua.replace("HeadlessChrome", "Chrome")
        # pretend to be Windows, as brozzler's stealth JS does (otherwise it's inconsistent)
        ua = ua.replace("(X11; Linux x86_64)", "(Windows NT 10.0; Win64; x64)")
        if not job.stealth_ua:
            # add mnbot link
            ua += f" (mnbot {VERSION}; +{job.mnbot_info_url})"
        logger.debug(f"using updated user agent {ua}")

        logger.debug("writing item info")
        self._write_warcprox_record(
            "metadata:mnbot-job-metadata",
            "application/json",
            json.dumps({
                "job": job.full_job,
                "version": VERSION
            }).encode(),
            job.warc_prefix
        )
        canon_url = str(brozzler.urlcanon.semantic(job.url))

        def on_screenshot(data):
            self._on_screenshot(data, canon_url, job.warc_prefix)

        # Shamelessly stolen from brozzler's Worker._browse_page.
        # Apparently service workers don't get added to the right WARC:
        # https://github.com/internetarchive/brozzler/issues/140
        # This fetches them with requests to work around it.
        already_fetched = set()
        def _on_service_worker_version_updated(chrome_msg):
            url = chrome_msg.get("params", {}).get("versions", [{}])[0].get("scriptURL")
            if url and url not in already_fetched:
                logger.info(f"fetching service worker script {url}")
                self._best_effort_proxy_url(url, extra_headers)
                already_fetched.add(url)

        logger.debug("browsing page")
        final_url, outlinks = browser.browse_page(
            page_url = job.url,
            user_agent = ua,
            skip_youtube_dl = True,
            # We do these two things manually so they happen after custom_js
            skip_extract_outlinks = True,
            skip_visit_hashtags = True,
            on_screenshot = on_screenshot,
            on_service_worker_version_updated = _on_service_worker_version_updated,
            stealth = True,
            extra_headers = extra_headers
        )
        assert len(outlinks) == 0, "Brozzler didn't listen to us :["
        status_code: int = browser.websock_thread.page_status

        # This is different than brozzler's built-in behaviour_dir thingy because
        # we actually save the output.
        custom_js_result = None
        if job.custom_js:
            logger.debug("running custom behaviour")
            message = self._run_cdp_command(
                browser = browser,
                method = "Runtime.evaluate",
                params = {
                    "expression": job.custom_js,
                    # Allow let redeclaration and await
                    # Let redeclaration isn't really necessary, but top-level await is nice.
                    "replMode": True,
                    # Makes it actually return the value
                    "returnByValue": True,
                }
            )
            try:
                custom_js_result = {
                    "status": "success",
                    "remoteObject": message['result']['result'],
                    "exceptionDetails": message['result'].get("exceptionDetails")
                }
                if custom_js_result.get("exceptionDetails") is not None:
                    custom_js_result['status'] = "exception"
            except KeyError:
                logger.error(f"unreadable response: {message}")
                custom_js_result = {
                    "status": "unknown",
                    "fullResult": message.get("result")
                }

        logger.debug("extracting outlinks")
        outlinks = browser.extract_outlinks()
        logger.debug("visiting anchors")
        browser.visit_hashtags(final_url, [], outlinks)

        # Dump the DOM
        # First get the root node ID using DOM.getDocument (which gets the root node).
        logger.debug("getting root node ID")
        root_node_id = self._run_cdp_command(browser, "DOM.getDocument")['result']['root']['nodeId']
        # Now get the outer HTML of the root node.
        logger.debug(f"getting outer HTML for node {root_node_id}")
        outer_html = self._run_cdp_command(browser, "DOM.getOuterHTML", {"nodeId": root_node_id})['result']['outerHTML']
        # And write it to the WARC.
        logger.debug("writing outer HTML to WARC")
        self._write_warcprox_record("rendered-dom:" + canon_url, "text/html", outer_html.encode(), job.warc_prefix)

        r = Result(
            final_url = final_url,
            outlinks = list(outlinks),
            custom_js_result = custom_js_result,
            status_code = status_code
        )
        logger.debug("writing job result data")
        self._write_warcprox_record(
            "metadata:mnbot-job-result",
            "application/json",
            json.dumps({
                "result": r.dict()
            }).encode(),
            job.warc_prefix
        )

        return r

    def _run_job_target(self, job: Job) -> Result:
        logger.debug(f"spun up thread for job {job.full_job['id']}")
        browser = self.pool.acquire()
        try:
            return self._brozzle(browser, job)
        finally:
            browser.stop()
            self.pool.release(browser)

    async def run_job(self, ws: "Websocket", full_job: dict, url: str, warc_prefix: str, stealth_ua: bool, custom_js: typing.Optional[str], info_url: str):
        tries = full_job['tries']
        id = full_job['id']
        #dedup_bucket = f"dedup-{id}-{tries}"
        dedup_bucket = ""
        stats_bucket = f"stats-{id}-{tries}"
        job = Job(
            full_job = full_job,
            url = url,
            warc_prefix = warc_prefix,
            dedup_bucket = dedup_bucket,
            stats_bucket = stats_bucket,
            stealth_ua = stealth_ua,
            custom_js = custom_js,
            cookie_jar = None,
            mnbot_info_url = info_url
        )
        result = await asyncio.to_thread(
            self._run_job_target,
            job = job
        )
        await ws.store_result(id, "status_code", tries, result.status_code)
        await ws.store_result(id, "outlinks", tries, result.outlinks)
        await ws.store_result(id, "final_url", tries, result.final_url)
        if result.status_code >= 400:
            raise RuntimeError(f"Bad status code {result.status_code}")
        if jsr := result.custom_js_result:
            await ws.store_result(id, "custom_js", tries, jsr)
            if jsr['status'] != "success":
                raise RuntimeError("Custom JS didn't succeed")
        return dedup_bucket, stats_bucket

async def warcprox_cleanup():
    conn = None
    try:
        conn = await r.connect(host = "rethinkdb")
        # We don't currently clean up the stats bucket as warcprox does it in batches, so when
        # this function runs, warcprox is going to re-add it in a few seconds anyway.
        logger.debug("deleting old dedup records")
        res = await (
            _dedup_db()
            # Deletes records more than 7 days old, to prevent the database from blowing up
            # and to make sure that corrupt records don't forever cause a URL to be lost
            .between(r.minval, r.now() - 7*24*3600, index = "mnbot-date")
            .delete(durability = "soft")
            .run(conn)
        )
        logger.debug(f"queued {res['deleted']} old dedup records for deletion")
    except Exception:
        logger.exception(f"failed to clean up old records")
    finally:
        try:
            if conn:
                await conn.close()
        except Exception:
            pass
