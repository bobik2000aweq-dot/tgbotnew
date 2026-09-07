HLUSTYAK BOT — WINDOWS EXE
==========================

1. Install Python 3.11 or newer for Windows.
2. Put all files from this folder into one directory.
3. Double-click build_windows.bat.
4. The finished one-file executable will be:
   dist\hlustyak_bot.exe

Before launching the executable, configure the required environment variables.
Do not put real secrets into this folder or into the source code.

Required:
  TELEGRAM_BOT_TOKEN
  DATABASE_URL (or NEON_DATABASE_URL)

Used by the AI screening feature:
  AI_INTEGRATIONS_OPENAI_API_KEY
  AI_INTEGRATIONS_OPENAI_BASE_URL (optional)

Configured for Huntme:
  HUNTME_API_KEY
  HUNTME_API_BASE_URL (optional; defaults to https://apihmscout.com/api/employee-api-key)
  HUNTME_OPERATOR_OFFICE_ID (required for operator applications)
  HUNTME_TIMEOUT_SECONDS (optional; defaults to 20)

Example for the current Windows terminal:
  set TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_TOKEN
  set DATABASE_URL=YOUR_DATABASE_URL
  set HUNTME_API_KEY=YOUR_EMPLOYEE_API_KEY
  set HUNTME_OPERATOR_OFFICE_ID=12
  set AI_INTEGRATIONS_OPENAI_API_KEY=YOUR_OPENAI_KEY
  dist\hlustyak_bot.exe


Only operator applications are synchronized with CRM: when an operator selects an interview slot, the bot checks it against the CRM and sends it to POST /request-call/operator. Scout applications stay inside the bot and are not sent to CRM. The local application is saved first, so a CRM outage does not lose it. An administrator can use /huntme_offices to list accessible office IDs.

The image is bundled into the executable by PyInstaller, so welcome.png is
needed only while building and does not need to be distributed with the EXE.