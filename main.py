"""
HOD Missing Daily Report Reminder - Vercel version
--------------------------------------------------
Every day at ~11:00 AM IST (Vercel Cron), this checks the #all-hods channel
for the PREVIOUS day. Any HOD who did not post any message that day gets a
polite reminder by private DM.

Rules (as specified):
- HODs = all human members of the channel, EXCEPT the IDs in EXCLUDED_USER_IDS.
- "Submitted" = posted any message in the channel during that day (IST).
- If the previous day was Sunday, the run is skipped (Sunday is off).
- Reminder is sent as a private DM to each defaulter.

Deployment notes:
- Vercel Cron calls GET /check (see vercel.json). On the Hobby plan the cron
  fires once per day, sometime within the hour after the scheduled time.
- If a CRON_SECRET env var is set, requests must carry
  "Authorization: Bearer <CRON_SECRET>" (Vercel adds this automatically for
  cron invocations when the env var exists).
- Set DRY_RUN=1 to log who WOULD be reminded without sending any DMs
  (recommended for the first test).
"""

import os
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hod-report-reminder")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
HOD_CHANNEL_ID = os.environ.get("HOD_CHANNEL_ID", "C0BDA3WDTJS")
LOCAL_TIMEZONE = os.environ.get("LOCAL_TIMEZONE", "Asia/Kolkata")

# Slack member IDs that must NEVER receive a reminder (Davinder Bisht,
# Gurmeet Singh, PC). ID matching is exact and immune to profile-name
# changes. Override with a comma-separated EXCLUDED_USER_IDS env var.
EXCLUDED_USER_IDS = {
    u.strip()
    for u in os.environ.get(
        "EXCLUDED_USER_IDS", "U0BBRLUN0UB,U0BBUKGQR7X,U0BBGH2CWGP"
    ).split(",")
    if u.strip()
}

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

MESSAGE_TEMPLATE = (
    "Dear {name},\n\n"
    "Thank you for your continued efforts and commitment towards maintaining "
    "high academic and operational standards.\n\n"
    "CMD Sir would like to understand if there are any challenges being faced "
    "in submitting the Daily HOD Report as per the prescribed reporting "
    "process. The report plays an important role in ensuring effective "
    "coordination, timely support, and smooth functioning across all "
    "departments.\n\n"
    "Kindly ensure that the Daily HOD Report is submitted within the "
    "stipulated timeline on a daily basis. As per the reporting system, "
    "non-submission attracts a score of -10; however, our primary objective "
    "is to maintain consistency, accountability, and effective communication "
    "rather than focus on deductions.\n\n"
    "Your cooperation and discipline in following the reporting process are "
    "highly appreciated and contribute significantly to the overall success "
    "of the institution.\n\n"
    "Regards,\n"
    "Core Team\n"
    "GCS Group"
)

client = WebClient(token=SLACK_BOT_TOKEN)

# IMPORTANT: Vercel looks for a Flask instance named exactly "app".
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_channel_members(channel_id):
    """All member user IDs of the channel (paginated)."""
    members = []
    cursor = None
    while True:
        resp = client.conversations_members(channel=channel_id, cursor=cursor, limit=200)
        members.extend(resp["members"])
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    return members


def get_user_profile(user_id):
    """Returns (is_human, real_name, display_name) for a user id."""
    try:
        u = client.users_info(user=user_id)["user"]
    except SlackApiError as e:
        logger.warning("users_info failed for %s: %s", user_id, e)
        return False, user_id, user_id
    if u.get("is_bot") or u.get("deleted") or u.get("id") == "USLACKBOT":
        return False, "", ""
    profile = u.get("profile", {})
    real_name = (u.get("real_name") or profile.get("real_name") or "").strip()
    display_name = (profile.get("display_name") or "").strip()
    return True, real_name or u.get("name", user_id), display_name


def is_excluded(user_id):
    return user_id in EXCLUDED_USER_IDS


def get_posters_between(channel_id, oldest_ts, latest_ts):
    """Set of user IDs who posted any message in the channel in the window."""
    posters = set()
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=channel_id,
            oldest=str(oldest_ts),
            latest=str(latest_ts),
            inclusive=True,
            limit=200,
            cursor=cursor,
        )
        for msg in resp.get("messages", []):
            # Skip join/leave notices etc., but keep normal messages and
            # file shares (subtype "file_share" counts as a report).
            subtype = msg.get("subtype", "")
            if subtype in ("channel_join", "channel_leave", "channel_topic",
                           "channel_purpose", "channel_name", "bot_message"):
                continue
            uid = msg.get("user")
            if uid:
                posters.add(uid)
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not (resp.get("has_more") and cursor):
            break
    return posters


def send_dm(user_id, name):
    dm = client.conversations_open(users=user_id)
    channel_id = dm["channel"]["id"]
    client.chat_postMessage(channel=channel_id, text=MESSAGE_TEMPLATE.format(name=name))


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------
def run_check():
    tz = ZoneInfo(LOCAL_TIMEZONE)
    now = datetime.now(tz)
    report_day = (now - timedelta(days=1)).date()

    if report_day.weekday() == 6:  # Sunday
        logger.info("Previous day %s was Sunday -- skipping (Sunday is off).", report_day)
        return {"status": "skipped", "reason": "previous day was Sunday", "day": str(report_day)}

    day_start = datetime.combine(report_day, dtime.min, tzinfo=tz).timestamp()
    day_end = datetime.combine(report_day, dtime.max, tzinfo=tz).timestamp()

    logger.info("Checking reports for %s in channel %s", report_day, HOD_CHANNEL_ID)
    posters = get_posters_between(HOD_CHANNEL_ID, day_start, day_end)
    logger.info("Users who posted on %s: %s", report_day, sorted(posters))

    reminded = []
    skipped_excluded = []
    submitted = []
    errors = []

    for uid in get_channel_members(HOD_CHANNEL_ID):
        is_human, real_name, display_name = get_user_profile(uid)
        if not is_human:
            continue
        if is_excluded(uid):
            skipped_excluded.append(real_name or display_name)
            continue
        if uid in posters:
            submitted.append(real_name)
            continue
        name = real_name or display_name or "HOD"
        if DRY_RUN:
            logger.info("[DRY RUN] Would DM reminder to %s (%s)", name, uid)
            reminded.append(name + " (dry-run)")
            continue
        try:
            send_dm(uid, name)
            logger.info("Reminder DM sent to %s (%s)", name, uid)
            reminded.append(name)
        except SlackApiError as e:
            logger.error("Failed to DM %s (%s): %s", name, uid, e)
            errors.append(name + ": " + str(e))

    summary = {
        "status": "ok",
        "report_day": str(report_day),
        "submitted": submitted,
        "reminded": reminded,
        "excluded": skipped_excluded,
        "errors": errors,
        "dry_run": DRY_RUN,
    }
    logger.info("Summary: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/check", methods=["GET", "POST"])
def check():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + CRON_SECRET:
            return jsonify({"status": "unauthorized"}), 401
    try:
        return jsonify(run_check())
    except Exception as e:
        logger.exception("run_check failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return "HOD Report Reminder is running.", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
