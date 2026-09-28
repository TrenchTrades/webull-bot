"""Optional Discord notifications (set DISCORD_WEBHOOK_URL). Never lets a failure stop the bot."""
import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def notify(webhook: str, message: str) -> None:
    log.info("NOTIFY: %s", message)
    if not webhook:
        return
    try:
        req = urllib.request.Request(webhook, data=json.dumps({"content": message[:1900]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "webull-bot"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log.warning("notification failed: %s", e)
