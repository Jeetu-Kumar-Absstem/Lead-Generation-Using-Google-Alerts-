import csv
import email
import imaplib
import os
import re

from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urldefrag
from html import unescape
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

OUTPUT_DIR = Path("google_alerts_output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Use India Standard Time for the daily date boundary.
IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).date()

# IMAP date search is used to narrow the search to today's date.
# Python also validates each message's INTERNALDATE in IST below.
SINCE_DATE = TODAY.strftime("%d-%b-%Y")
TOMORROW_DATE = (TODAY + timedelta(days=1)).strftime("%d-%b-%Y")

# Each run gets its own files, preserving previous results.
RUN_ID = datetime.now(IST).strftime("%Y%m%d_%H%M%S")

LINKS_FILE = OUTPUT_DIR / f"google_alerts_links_{RUN_ID}.csv"
RAW_FILE = OUTPUT_DIR / f"google_alerts_raw_{RUN_ID}.txt"


def decode_header_value(value):
    if not value:
        return ""

    decoded = []

    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            decoded.append(
                part.decode(encoding or "utf-8", errors="replace")
            )
        else:
            decoded.append(part)

    return "".join(decoded)


def extract_body(message):
    bodies = []

    parts = (
        message.walk()
        if message.is_multipart()
        else [message]
    )

    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue

        if part.get_content_type() not in (
            "text/plain",
            "text/html",
        ):
            continue

        payload = part.get_payload(decode=True)

        if payload is None:
            continue

        charset = part.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
        bodies.append(body)

    return "\n\n".join(bodies)


def extract_urls(content):
    content = unescape(content)

    found = re.findall(
        r'https?://[^\s<>"\']+',
        content,
        flags=re.IGNORECASE,
    )

    unique_urls = []
    seen = set()

    for url in found:
        url = url.rstrip(".,;:!?)\u005d")

        # Remove fragments for basic duplicate detection.
        normalized, _ = urldefrag(url)

        if not urlparse(normalized).netloc:
            continue

        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(url)

    return unique_urls


def get_internal_date_ist(fetch_response):
    """
    Extract the message INTERNALDATE returned by Gmail IMAP
    and convert it to an IST date.
    """
    for item in fetch_response:
        if not isinstance(item, tuple):
            continue

        metadata = item[0]

        if isinstance(metadata, bytes):
            metadata = metadata.decode("utf-8", errors="replace")

        match = re.search(
            r'INTERNALDATE "([^"]+)"',
            metadata,
            flags=re.IGNORECASE,
        )

        if match:
            received_at = parsedate_to_datetime(match.group(1))

            if received_at.tzinfo is None:
                # Gmail normally supplies a timezone offset.
                # Treat a missing offset as UTC rather than local time.
                from datetime import timezone
                received_at = received_at.replace(tzinfo=timezone.utc)

            return received_at.astimezone(IST)

    return None


def main():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "Missing Gmail credentials in the .env file."
        )

    total_emails = 0
    total_links = 0
    skipped_wrong_date = 0
    skipped_fetch_failed = 0

    print("\nABSSTEM GOOGLE ALERTS READER")
    print("-" * 40)
    print(f"Today's date (IST): {TODAY}")
    print(f"Searching date: {SINCE_DATE} to before {TOMORROW_DATE}")
    print("Sender: googlealerts-noreply@google.com")
    print("-" * 40)

    with open(
        LINKS_FILE,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as csv_file, open(
        RAW_FILE,
        "w",
        encoding="utf-8",
    ) as raw_file:

        columns = [
            "message_id",
            "date",
            "sender",
            "subject",
            "url",
        ]

        writer = csv.DictWriter(
            csv_file,
            fieldnames=columns,
        )
        writer.writeheader()

        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            993,
        )

        try:
            mail.login(
                GMAIL_ADDRESS,
                GMAIL_APP_PASSWORD,
            )

            status, _ = mail.select(
                "INBOX",
                readonly=True,
            )

            if status != "OK":
                raise RuntimeError(
                    "Could not open Gmail inbox."
                )

            # Search only Google Alerts messages whose Gmail date
            # falls on today's IST calendar date.
            status, data = mail.search(
                None,
                "SINCE", SINCE_DATE,
                "BEFORE", TOMORROW_DATE,
                "FROM", "googlealerts-noreply@google.com",
            )

            if status != "OK":
                raise RuntimeError(
                    "Gmail search failed."
                )

            message_ids = data[0].split()
            print(f"Candidate emails found: {len(message_ids)}")

            for message_id in message_ids:
                # Fetch INTERNALDATE as well as the email body.
                status, response = mail.fetch(
                    message_id,
                    "(INTERNALDATE BODY.PEEK[])",
                )

                if status != "OK":
                    skipped_fetch_failed += 1
                    continue

                received_at_ist = get_internal_date_ist(response)

                # Strictly include only messages received today in IST.
                if received_at_ist is None:
                    print(
                        f"Skipping message {message_id.decode(errors='replace')}: "
                        "could not read INTERNALDATE."
                    )
                    skipped_wrong_date += 1
                    continue

                if received_at_ist.date() != TODAY:
                    skipped_wrong_date += 1
                    continue

                raw_email = next(
                    (
                        item[1]
                        for item in response
                        if isinstance(item, tuple)
                        and isinstance(item[1], bytes)
                    ),
                    None,
                )

                if not raw_email:
                    skipped_fetch_failed += 1
                    continue

                message = email.message_from_bytes(raw_email)

                subject = decode_header_value(
                    message.get("Subject")
                )
                sender = message.get("From", "")
                date = message.get("Date", "")
                body = extract_body(message)

                urls = extract_urls(body)

                total_emails += 1
                total_links += len(urls)

                message_id_text = message_id.decode(
                    "utf-8",
                    errors="replace",
                )

                # Preserve the original message content for analysis.
                raw_file.write("\n" + "=" * 80 + "\n")
                raw_file.write(
                    f"Message ID: {message_id_text}\n"
                )
                raw_file.write(
                    f"Received (IST): {received_at_ist.isoformat()}\n"
                )
                raw_file.write(f"Date: {date}\n")
                raw_file.write(f"From: {sender}\n")
                raw_file.write(f"Subject: {subject}\n")
                raw_file.write("-" * 80 + "\n")
                raw_file.write(body + "\n")

                # One CSV row per URL; retain emails with no links
                # so they can be reviewed too.
                for url in urls or [""]:
                    writer.writerow({
                        "message_id": message_id_text,
                        "date": date,
                        "sender": sender,
                        "subject": subject,
                        "url": url,
                    })

        finally:
            try:
                mail.logout()
            except Exception:
                pass

    print("\nGoogle Alerts extraction complete.")
    print(f"Emails processed: {total_emails}")
    print(f"Links extracted: {total_links}")
    print(f"Skipped (not today / date unavailable): {skipped_wrong_date}")
    print(f"Skipped (fetch failed): {skipped_fetch_failed}")
    print(f"Links CSV: {LINKS_FILE.resolve()}")
    print(f"Raw emails: {RAW_FILE.resolve()}")


if __name__ == "__main__":
    main()
