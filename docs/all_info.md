# Automations, Apps & Widgets — How They Work

<aside>
🧩 This is the living index of our tech stack: automations, apps, widgets, and workflows. Source data: Monday board ‘Existing Tech’ (18142222315).

</aside>

## How to use this doc

- Use Ctrl/Cmd+F to find a system.
- Each entry includes: purpose, where it runs, owner, dependencies, guide link, and how to trigger/verify it.

## Index (by platform)

We’ll keep the canonical list in Monday, but this page is optimized for humans.

## Heroku

- Duplicate-Combinator — Live
    
    Scans through specific Monday.com boards, and set groups, looking for item names that it believes to be associated with an existing item for the same project
    
    - Owner: Tommy Lather
    - Platform: Heroku, Heroku DB
    - Primary language: Python (runtime 3.12.5)
    - Dependencies: Monday API, Flask, Gunicorn
    - [Guide](https://takeoffmonkey.monday.com/protected_static/8540396/resources/2477966390/Duplicate_Combinator_Guide_v10.pdf)
    - [GitHub](https://github.com/Takeoff-Monkey/duplicate_combinator)
    - [App link](https://duplicate-combinator-f871de7ce7b1.herokuapp.com/settings?token=48146d18922189d929729ecedcee37bcbb86d4eb90f25df10bb4edc17b97d93e)
- Ewing Revision Creator — Live
    
    Slack Bot that finds existing completed job, creates another Monday item of the item as a numbered revision, recycles google doc instructions by listening for '#revise!' followed by the job number '#12345!'
    
    - Owner: Tommy Lather
    - Platform: Heroku, Slack
    - Primary language: Python (runtime 3.13)
    - Dependencies: Monday API, Google Drive, Google Sheets, Google Docs, Slack API
    - [Guide](https://takeoffmonkey.monday.com/protected_static/8540396/resources/2473012318/Slack_Ewing_Rev_Creator_v1.pdf)
    - [GitHub](https://github.com/Takeoff-Monkey/ewing_rev_creator)
    - App link: —
- Ewing Note Updater — Live
    
    Slack Bot which appends specific slack messages to google doc instructions doc by listening for the job # in format '#12345!'
    
    - Owner: Tommy Lather
    - Platform: Heroku, Slack
    - Primary language: Python, Google App Script (runtime 3.13)
    - Dependencies: Monday API, Google Sheets, Google Drive, Google Docs, Google App Script
    - [Guide](https://takeoffmonkey.monday.com/protected_static/8540396/resources/2473010511/Slack_Note_Updater_Guide_v1b.pdf)
    - [GitHub](https://github.com/Takeoff-Monkey/slack_note_updater)
    - App link: —
- Ewing Design Right-Sizer — Live
    
    Slack bot that compresses PDF design files to <~20-25mb
    
    - Owner: Tommy Lather
    - Platform: Heroku
    - Primary language: Python (runtime 3.13)
    - Dependencies: Google API, Flask, Slack API
    - [Guide](https://takeoffmonkey.monday.com/protected_static/8540396/resources/2473012871/Slack_Design_Right_sizer_v1.0.pdf)
    - [GitHub](https://github.com/Takeoff-Monkey/design-right-sizer)
    - [App link](https://git.heroku.com/design-right-sizer.git)
- DataChimp — Live
    
    OpenAI + PandasAI Slack bot that can answer questions on our worklog history, produce tables, charts, analytics
    
    - Owner: Tommy Lather
    - Platform: Heroku, DynamoDB, Slack
    - Primary language: Python (runtime 3.11.9)
    - Dependencies: OpenAI, PandasAI, DynamoDB, Slack API
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/dataChimp/tree/main)
    - [App link](https://git.heroku.com/datachimp.git)
- Ewing Auto-Instructor — In Progress
    
    Background bot that automatically inserts branch, customer, regional, and sales rep standard requirements to the instruction google doc
    
    - Owner: Tommy Lather
    - Platform: Heroku
    - Primary language: Python (runtime 3.13)
    - Dependencies: Google Sheets, Google Drive, Google Docs
    - Guide: —
    - [GitHub](https://github.com/TommytheMonkey/auto_instruct)
    - App link: —
- Question Relay — Live
    
    Slack Bot which logs questions from the team to a google sheet, tracks the question being manually relayed to the client, and automatically relays the client's response back to the production team-member user who originally asked the question.
    
    - Owner: Tommy Lather
    - Platform: Heroku, Slack
    - Primary language: Python (runtime 3.12.5)
    - Dependencies: Slack API, Monday API
    - [Guide](https://takeoffmonkey.monday.com/protected_static/8540396/resources/2473015190/Question%20Relay%20Guide%20v1.0.pdf)
    - [GitHub](https://git.heroku.com/ewing-question-relay.git)
    - [App link](https://git.heroku.com/ewing-question-relay.git)
- Estimator's Bid Notes
    
    Scans drawings, PDF takeoff, worksheet, and returns a 1-2 page summary of the job including pertinent notes for that customer's specific scope(s) of work, drops into a *.md file which is saved to the project sales folder as well as emailed to them
    
    - Owner: —
    - Platform: Heroku
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —
- Catalog Matcher
    
    Compares our material list with a customer's catalog and matches items appropriately
    
    - Owner: —
    - Platform: Heroku
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —

## Zapier

- MTO Processor Assignment — Live
    
    Automatically assigns reviewer based on client, branch, and product type
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier
    - Guide: —
    - GitHub: —
    - App link: —
- Dropbox Folder Builder — Live
    
    Creates day, client, and job folders in purgatory as projects hit our inbox
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Dropbox, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Projects inbox >>> Monday loader — Live
    
    Automatically creates items in Monday WLIII from emails sent to projects@takeoffmonkey.com
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Gmail, Zapier, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- HLS Note Bot — Live
    
    Grabs notes from giddyup (client platform) and drops them into their respective Monday items as updates
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Ewing Auto Loader — Live
    
    Creates items in Monday and populates various columns with structured data contained in emails sent to ewing@takeoffmonkey.com
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Night Watchman Ewing — Live
    
    Alerts customers via Slack if a project is submitted AFTER the cut-off time (6:30pm CST)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Monday, Slack API
    - Guide: —
    - GitHub: —
    - App link: —
- Embassy BB Loader — Live
    
    Populates items on customer bid board from emails sent to embassy@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- HLS - Muzaffar Assignment — Live
    
    Assigns completed irrigation designs for a specific client to Muzaffar for final review
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Monday, Zapier
    - Guide: —
    - GitHub: —
    - App link: —
- Due Date vs. Customer Due Date — Live
    
    Alerts TOM US team if a due date is set that is ON or AFTER the client's actual due date
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Prime Lawn email loader — Live
    
    Populates items on customer bid board from emails sent to prime@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Ewing Inbox Checker (four separate automations for each region) — Live
    
    Alerts client of projects that have NOT been pushed into our queue at the end of each day near the region's cutoff time
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Monday, Slack API
    - Guide: —
    - GitHub: —
    - App link: —
- Ewing Delivered job Mover — Live
    
    Pushes job from 'Active' to delivered tab on back-up google sheet
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Google Sheets
    - Guide: —
    - GitHub: —
    - App link: —
- Prep Complete > Monday Assignment — Live
    
    Assigns prepper & production team based on client, product type, and region
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Projects Email Spam Notification — Live
    
    Alerts US team via Slack when an email hits the spam / junk folder
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Slack API
    - Guide: —
    - GitHub: —
    - App link: —
- Completed DocuSign Loader — Live
    
    Logs completed docusigns to Monday board
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, DocuSign, Monday, Gmail
    - Guide: —
    - GitHub: —
    - App link: —
- CLMS Inbox Loader — Live
    
    Populates items on customer bid board from emails sent to CLMS@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Demo BB Loader — Live
    
    (For demonstration purposwes) Populates items on customer bid board from emails sent to yourcompany@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Blogger — Live
    
    Takes a blog topic / idea from a Monday board along with any specific information and creates a blog article in google docs
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, OpenAI, Monday, Gemini, Google Docs
    - Guide: —
    - GitHub: —
    - App link: —
- NuStyle Inbox Loader — Live
    
    Populates items on customer bid board from emails sent to nustyle@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- JMI Inbox Loader — Live
    
    Populates items on customer bid board from emails sent to JMI@takeoffmonkey.com (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Roebuck Maint - Gmail Integration — Live
    
    Populates items on customer bid board from emails sent to roebuck@takeoffmonkey.com or roebuck_maint (alias of bids@..)
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —
- Website contact form grabber — Live
    
    Listens for emails generated by form submission on website & logs them to a Monday board
    
    - Owner: Tommy Lather
    - Platform: Zapier
    - Primary language: —
    - Dependencies: Zapier, Gmail, Monday
    - Guide: —
    - GitHub: —
    - App link: —

## AWS

- prod-lambda-itb-n8n-large-file-handler-2025-10-08 — Live
    
    (no description yet)
    
    - Owner: Tommy Lather
    - Platform: AWS Lambda
    - Primary language: Node.js (runtime 22.x)
    - Dependencies: AWS
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-itb-n8n-large-file-handler-2025-10-08)
- prod-lambda-Dropbox-db-import-2025-10-08 — Live
    
    Full workflow: importing Excel data from Dropbox into DynamoDB
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Database-Importer)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Dropbox-db-import-2025-10-08)
- prod-lambda-default-db-worker-2025-10-08 — Live
    
    Data manipulation & exporting ONLY for Dropbox to DynamoDB workflow
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-default-db-worker-2025-10-08)
- prod-lambda-Bid-Downloader-worker-2025-10-08 — Live
    
    (no description yet)
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Email-Scanner)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Bid-Downloader-worker-2025-10-08)
- prod-lambda-CTX-updater-2025-10-08 — Live
    
    (no description yet)
    
    - Owner: Tommy Lather
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: Monday API, DynamoDB
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-CTX-updater-2025-10-08)
- prod-lambda-Highlight-Pipeline-PDF-2025-10-08 — Live
    
    Run CI/CD pipeline on PDF highlighter (should this be deprecated?)
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: AWS
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Highlight-Pipeline-PDF-2025-10-08)
- prod-lambda-Extractor-Pipeline-PDF-2025-10-08 — Live
    
    Run CI/CD pipeline on schedule extractor (should this be deprecated?)
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: AWS
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Extractor-Pipeline-PDF-2025-10-08)
- prod-lambda-Everde-db-import-2025-10-08 — Live
    
    Specialized import flow from Everde to DynamoDB
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Everde-db-import-2025-10-08)
- prod-Worklog-updater-2025-10-08 — Live
    
    Update the Worklog III DynamoDB w/ Monday.com data
    
    - Owner: Konur Papageorgiou, Tommy Lather
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: Monday API, DynamoDB
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/WorkLog_Scheduler)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-Worklog-updater-2025-10-08)
- prod-lambda-get-order-info-2025-10-08 — Live
    
    (no description yet)
    
    - Owner: Tommy Lather
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-get-order-info-2025-10-08)
- prod-lambda-Scan-Pipeline-PDF-2025-10-08 — Live
    
    Run CI/CD pipeline on PDF scanner (should this be deprecated?)
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: AWS
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Scan-Pipeline-PDF-2025-10-08)
- prod-lambda-Arazoza-db-worker-2025-10-08 — Live
    
    Data manipulation & exporting ONLY for Dropbox to DynamoDB workflow specially for Arazoza
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Arazoza-db-worker-2025-10-08)
- prod-lambda-Worklog-III-manual-refresh-2025-10-08 — Live
    
    (no description yet)
    
    - Owner: Tommy Lather
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: Monday API, DynamoDB
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-Worklog-III-manual-refresh-2025-10-08)
- prod-lambda-IS-db-worker-2025-10-08 — Live
    
    Data manipulation & exporting ONLY for Dropbox to DynamoDB workflow specially for Irrigation Station
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API
    - Guide: —
    - GitHub: —
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/prod-lambda-IS-db-worker-2025-10-08)
- test-lambda-Database-Import-1-2025-10-08 — Live
    
    Full data import from Dropbox into DynamoDB; test workflow, NOT production
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API, AWS
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Database-Importer)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/test-lambda-Database-Import-1-2025-10-08)
- test-lambda-Database-Import-2-2025-10-08 — Live
    
    Full data import from Dropbox into DynamoDB; test workflow, NOT production
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, AWS, Dropbox API
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Database-Importer)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/test-lambda-Database-Import-2-2025-10-08)
- test-lambda-Database-Import-3-2025-10-08 — Live
    
    Full data import from Dropbox into DynamoDB; test workflow, NOT production
    
    - Owner: Konur Papageorgiou
    - Platform: AWS Lambda
    - Primary language: Python (runtime 3.13)
    - Dependencies: DynamoDB, Dropbox API, AWS
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Database-Importer)
    - [App link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/test-lambda-Database-Import-3-2025-10-08)
- test-Neon-updater-2026-01-20
    
    Runs on a cron schedule to automatically extract new bid emails and import into Neon DB
    
    - Owner: —
    - Platform: AWS
    - Primary language: —
    - Dependencies: AWS, Gmail, Google API, Neon
    - Guide: —
    - GitHub: —
    - App link: —
- prod-lambda-Gmail-to-Neon-updater-2026-01-28
    
    One-time manual data backport from original DynamoDB into new DynamoDB
    
    - Owner: —
    - Platform: AWS
    - Primary language: —
    - Dependencies: AWS, DynamoDB, Monday
    - Guide: —
    - GitHub: —
    - App link: —

## Google Cloud

- Streamlit Widget Application — Live
    
    Streamlit widgets hub
    
    - Owner: Konur Papageorgiou
    - Platform: GCP
    - Primary language: Python (runtime 3.11.2)
    - Dependencies: Streamlit, AWS, OpenAI, Monday API
    - Guide: —
    - [GitHub](https://github.com/Takeoff-Monkey/Streamlit-Widget-Application)
    - [App link](https://www.takeoffmonkey.app)

## Appwrite

- Takeoff Monkey Widgets — In Progress
    
    Widgets site in React hosted in GCP Cloud Run, functions & API in Appwrite
    
    - Owner: Konur Papageorgiou
    - Platform: Appwrite
    - Primary language: React/JS (runtime 18.3.1)
    - Dependencies: Appwrite, Gemini, AWS, OpenAI, React
    - [Guide](https://console.cloud.google.com/run/detail/us-east1/takeoff-monkey-widgets/revisions?authuser=0&hl=en&inv=1&invt=Ab1glQ&project=g-sheets-experiments)
    - [GitHub](https://github.com/Takeoff-Monkey/Takeoff-Monkey-Widgets)
    - [App link](https://www.automatethat.app)

## Monday - Internal (Work Log III)

- Timestamps
    
    Set date & time an item is created
    
    - Owner: —
    - Platform: Monday - Internal (Work Log III)
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —
- Move to 'Active' Group
    
    When a project (item) is marked 'Uploaded', move project to 'Active' group
    
    - Owner: —
    - Platform: Monday - Internal (Work Log III)
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —
- Time tracking
    
    (no description yet)
    
    - Owner: —
    - Platform: Monday - Internal (Work Log III)
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —
- Build Folder
    
    (no description yet)
    
    - Owner: —
    - Platform: Monday - Internal (Work Log III)
    - Primary language: —
    - Dependencies: —
    - Guide: —
    - GitHub: —
    - App link: —

---

## If an automation breaks: what to collect before escalating

- What were you trying to do? (1 sentence)
- Where? (board name + link + item name + item ID)
- When? (timestamp + timezone)
- What changed right before it failed? (status change, button click, file upload, etc.)
- Expected result vs actual result
- Screenshots: the item + the relevant columns (before/after)
- Any error messages (Monday, Slack, email bounce, etc.)
- Links to artifacts: Dropbox/Drive folder, doc links, delivered email thread if applicable

Escalate to: Tommy (include the above so the fix is fast).

## Work Log III — built-in Monday automations

These are the automations configured directly on Work Log III. Source: Monday board ‘Existing Tech’ → group ‘Monday - Internal (Work Log III)’.

- Timestamps
    
    Trigger / how to use: status changes and/or key events update timestamp fields automatically.
    
    - Success signals: expected date columns (e.g., Rec’d/Comp/Delivered) auto-populate when the corresponding workflow step occurs.
    - Common failure modes: automation turned off; column renamed; status labels changed; permission changes.
    - When it breaks: collect item link + status history + which timestamp did not update.
- Move to ‘Active’ Group
    
    Trigger / how to use: when an item meets the ‘ready’ condition (typically assignment / send-to field), it moves from Inbox-style holding area into Active.
    
    - Success signals: item changes group automatically; status aligns with production flow.
    - Common failure modes: trigger column value changed; group renamed; automation conflict with another move rule.
- Time tracking
    
    Trigger / how to use: time tracking columns record work durations; used for productivity and billing analytics.
    
    - Success signals: timers start/stop as expected; totals roll up to reporting.
    - Common failure modes: users don’t have permissions; column changed; automation not applied to subitems.
- Build Folder
    
    Trigger / how to use: when ‘Build Folders’ is set, folder creation process should occur (Drive/Dropbox or internal folder structure depending on workflow).
    
    - Success signals: production folder link appears (e.g., Prod. Folder / Completed Docs link updates).
    - Common failure modes: downstream Zapier/Heroku dependency is down; missing permissions; naming mismatch; large file edge cases.
    - When it breaks: collect item ID + job number + whether Build Folders was set + any generated links.

---

## Runbooks (top automations) — trigger / verify / failure modes

- Dropbox Folder Builder (Zapier)
    
    Trigger / how to use: When a job is ready for folder creation (commonly via ‘Build Folders’ / production workflow), this automation creates and links the folder structure.
    
    - Verify success: ‘Prod. Folder’ and/or ‘Completed Docs’ links populate; folder exists in Dropbox with expected subfolders.
    - Common failures: naming mismatch; duplicate item; Dropbox permissions; Zapier task error; downstream rate limits.
    
    What to collect: item link + item ID + job number + timestamp + screenshot of Build Folders/Billing/Links columns + expected folder path. Escalate to Tommy.
    
- Projects inbox → Monday loader (Zapier)
    
    Trigger / how to use: New inbound request hits the projects inbox/form and is automatically converted into a Monday item.
    
    - Verify success: new item appears in the correct group/board with sender + files/links populated.
    - Common failures: malformed email; missing attachment/link; API throttling; board/group mapping changed.
    
    What to collect: original email subject + timestamp + sender + expected board + whether files were attached; item ID if created partially. Escalate to Tommy.
    
- Due Date vs Customer Due Date (Zapier)
    
    Trigger / how to use: When customer due date is set/changed, internal due date check/logic runs to keep expectations consistent.
    
    - Verify success: due-date check field updates; confirmation email logic aligns with requested vs internal due date.
    - Common failures: status labels changed; date columns renamed; timezone edge cases; customer edits after ‘sent to TOM’ not propagating.
    
    What to collect: item link + requested due date + confirmation email due date + screenshots of both date columns. Escalate to Tommy.
    
- Ewing Auto Loader (Zapier)
    
    Trigger / how to use: When an Ewing job arrives, loader creates/updates the job item + routes it into the correct workflow.
    
    - Verify success: item created/updated, job number set, and instructions/files attached in the expected columns.
    - Common failures: missing job number format; attachment too large; link requires auth; API quota.
    
    What to collect: job number + original source link/email + whether files > 25MB + item ID if created. Escalate to Tommy.
    
- Duplicate-Combinator (Heroku)
    
    Trigger / how to use: Periodically scans boards/groups to detect items that represent the same project and reconciles/flags duplicates.
    
    - Verify success: duplicates are flagged/merged per rules; fewer double-entries in queue.
    - Common failures: naming conventions drift; false positives; API rate limits; service down.
    
    What to collect: duplicate item links + why you believe they’re the same + timestamps + screenshots. Escalate to Tommy.
    

---

## Work Log III — Active board automations (non-webhook)

Source: exported active automations CSV for WLIII (board 3874058084). Webhook automations omitted unless their purpose is documented elsewhere.

### Gmail

- When Status changes from Pending to Uploaded, send an email to Email Address
    - Owner: Tommy Lather
    - Last updated: Jan 5, 2026, 9:56 AM
    - Automation ID: 240213300
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Due Date changes and only if Status is not Pending  send an email to Email Address
    - Owner: Tommy Lather
    - Last updated: Jun 5, 2023, 7:40 AM
    - Automation ID: 164415901
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When button clicked  send an email to ALL Recipients (Delivery) and  send an email to Email Address
    - Owner: Tommy Lather
    - Last updated: Mar 13, 2023, 11:18 AM
    - Automation ID: 162375199
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When an email is received, create an item in Inbox
    - Owner: Leah Papageorgiou
    - Last updated: Oct 21, 2025, 3:48 PM
    - Automation ID: 368133719
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    

### Jotform Form Builder - v4

- When a status changes to something, assign Irrigation Design Request Form to Email Address
    - Owner: Tommy Lather
    - Last updated: Mar 1, 2023, 10:22 AM
    - Automation ID: 168508967
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When a status changes to something, assign Basic MTO Scope Checklist to Email Address
    - Owner: Tommy Lather
    - Last updated: Mar 1, 2023, 10:28 AM
    - Automation ID: 166313877
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    

### Monday (native)

- When Status changes to PREP  move item to Active Projects (Uploaded / In Progress / Complete)
    - Owner: Priscilla Rosales
    - Last updated: Apr 14, 2025, 1:55 PM
    - Automation ID: 413163370
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to PREP  start Time Tracking
    - Owner: Tommy Lather
    - Last updated: Apr 24, 2025, 1:45 PM
    - Automation ID: 416917965
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Billing Status changes to Trial  and only if Status is Delivered create an item in New Client Trials
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 12:51 PM
    - Automation ID: 172637104
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Billing Status changes to Credit Block  and only if Status is Delivered create an item in Credit Block / Dedicated Resource - Billing
    - Owner: Amy MacPherson
    - Last updated: Apr 3, 2024, 12:52 PM
    - Automation ID: 178239038
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Billing Status changes to Dedicated Resource  and only if Status is Delivered create an item in Credit Block / Dedicated Resource - Billing
    - Owner: Amy MacPherson
    - Last updated: Apr 3, 2024, 12:52 PM
    - Automation ID: 178232757
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Complete  stop Time Tracking
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:15 AM
    - Automation ID: 161318504
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Delivered  set Delivered Date to today
    
    Auto Delivered Date
    
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:16 AM
    - Automation ID: 159116976
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to CANCELLED  move item to Cancelled Projects
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:16 AM
    - Automation ID: 164308853
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Uploaded  start Time Tracking
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:17 AM
    - Automation ID: 161318343
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to CANCELLED  move item to Cancelled Projects and stop Time Tracking
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:17 AM
    - Automation ID: 172611253
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Delivered  move item to Delivered Projects
    
    Auto Delivered Group
    
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:17 AM
    - Automation ID: 159117233
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to HOLD  stop Time Tracking
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:18 AM
    - Automation ID: 172610956
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to HOLD  move item to ON HOLD
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:18 AM
    - Automation ID: 183289568
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Complete  set Comp. Date to today
    
    Auto Comp. Date
    
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:18 AM
    - Automation ID: 159116751
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Delivered  and only if Billing Status is Trial create an item in New Client Trials
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:19 AM
    - Automation ID: 172601971
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Convo  move item to Conversation
    - Owner: Tommy Lather
    - Last updated: Apr 3, 2024, 5:19 AM
    - Automation ID: 160933056
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Uploaded  move item to Active Projects (Uploaded / In Progress / Complete)
    - Owner: Tommy Lather
    - Last updated: Aug 12, 2024, 8:40 AM
    - Automation ID: 159798448
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Reached out  move item to ON HOLD
    - Owner: Amy MacPherson
    - Last updated: Aug 20, 2024, 3:11 PM
    - Automation ID: 331198902
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to DUPLICATE  move item to Duplicates
    - Owner: Amy MacPherson
    - Last updated: Dec 9, 2025, 6:47 AM
    - Automation ID: 480934141
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When status changes to something set Rec'd Date to today
    
    Auto Rec'vd Date
    
    - Owner: Tommy Lather
    - Last updated: Jan 29, 2023, 1:02 PM
    - Automation ID: 159116647
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When an item is created set Rec'd Date to today
    - Owner: Tommy Lather
    - Last updated: Jan 29, 2023, 7:48 AM
    - Automation ID: 159798886
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When button clicked move item to Active Projects (Uploaded / In Progress / Complete)
    - Owner: Tommy Lather
    - Last updated: Jan 29, 2023, 7:49 AM
    - Automation ID: 159308817
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- 15 days after Rec'd Date arrives and only if item is in ON HOLD and only if Status is HOLD move item to Archive of WLIII
    - Owner: Tommy Lather
    - Last updated: Jan 5, 2024, 3:13 PM
    - Automation ID: 250395877
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- 2 days after Rec'd Date arrives and only if item is in Cancelled Projects and only if Status is CANCELLED move item to Archive of WLIII
    - Owner: Tommy Lather
    - Last updated: Jan 5, 2024, 3:19 PM
    - Automation ID: 250398502
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to In Process  and only if Status is HOLD move item to Active Projects (Uploaded / In Progress / Complete)
    - Owner: Amy MacPherson
    - Last updated: Jul 2, 2025, 12:39 PM
    - Automation ID: 442283212
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When button clicked set Status to Delivered
    - Owner: Tommy Lather
    - Last updated: Mar 1, 2023, 3:05 PM
    - Automation ID: 168628233
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Prep Complete  stop Prep
    - Owner: Tommy Lather
    - Last updated: May 1, 2025, 10:11 AM
    - Automation ID: 419255432
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Delivered  and only if From is Ratliff HS set To to Ratliff HS
    - Owner: Amy MacPherson
    - Last updated: May 2, 2025, 1:34 PM
    - Automation ID: 419758381
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Design Prep  move item to Design Prep (Ewing Only)
    - Owner: Tommy Lather
    - Last updated: May 7, 2025, 12:28 PM
    - Automation ID: 421373146
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Uploaded  and only if item is in ON HOLD move item to Active Projects (Uploaded / In Progress / Complete)
    - Owner: Leah Papageorgiou
    - Last updated: Oct 11, 2024, 5:08 PM
    - Automation ID: 311552562
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Complete  and only if To Process: is assigned to Priscilla Rosales notify Priscilla Rosales
    - Owner: Priscilla Rosales
    - Last updated: Sep 24, 2024, 10:13 AM
    - Automation ID: 342008791
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Client Name changes perform formula and cast result to Client Name (text, recast)
    - Owner: Tommy Lather
    - Last updated: Sep 29, 2025, 4:17 PM
    - Automation ID: 476657097
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Client Name changes , apply a formula and set the result to Client Name (text, recast)
    - Owner: Tommy Lather
    - Last updated: Sep 30, 2025, 5:10 AM
    - Automation ID: 476838353
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    
- When Status changes to Complete in Procore  set Comp. Date to today
    - Owner: Amy MacPherson
    - Last updated: Sep 6, 2024, 1:24 PM
    - Automation ID: 336746075
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    

### Slack

- When Status changes to Prep in Process   notify in general and start Prep
    - Owner: Tommy Lather
    - Last updated: May 8, 2025, 4:36 AM
    - Automation ID: 419253626
    
    Trigger / how to use: (describe what column/status/button change causes this to run)
    
    Verify success: (what should change in Monday / email / Slack)
    
    Common failure modes: permissions, renamed columns/status labels, missing emails, etc.
    
    What to collect before escalating: item link + item ID + timestamp + screenshots of relevant columns. Escalate to Tommy.
    

### Omitted (webhooks)

Omitted 20 webhook-based automations from this section (per instruction). If you want, we can add documented ones later with purpose + endpoint owner.

<aside>
📁 Note: We use Google Drive (not Dropbox) for production/delivery folders. Any references to Dropbox should be interpreted as Google Drive links/folders.

</aside>

---

## Chrome Extensions

- [Procore Worksheet Exporter (Chrome Extension)](https://www.notion.so/Procore-Worksheet-Exporter-Chrome-Extension-31bfac88147a81cd8de4f138505bc111?pvs=21)
    
    Adds an Export BOM button to Procore Estimating and downloads a clean CSV grouped by layer.
    
    - Platform: Chrome extension (Windows 11 + Chrome)
    - Owner: Tommy
    - Install/Usage guide: [Notion page](https://www.notion.so/Procore-Worksheet-Exporter-Chrome-Extension-31bfac88147a81cd8de4f138505bc111?pvs=21)
    - Repo: [TommytheMonkey/procore-table-export](https://github.com/TommytheMonkey/procore-table-export)
    
    Verify success: Export BOM button appears on Procore Estimating tab; CSV downloads; clipboard paste works.
    
    Common failures: Procore UI changes; rows not loaded (need scroll); extension unloaded after Chrome update.
    
    What to collect before escalating: Procore URL, screenshot of estimating table, console errors (if any), Chrome version.