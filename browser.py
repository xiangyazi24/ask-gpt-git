#!/usr/bin/env python3
"""Browser automation layer — stub.

In the full chatgpt-bridge, the Chrome extension (background worker +
content script) handles:

1. Polling /api/pending to pick up tasks for its channel
2. Injecting the question text into the ChatGPT input box
3. Clicking send
4. (In DOM mode) scraping the answer — NOT needed for git-drop

For ask-gpt-git, the extension still does steps 1-3. The server just
needs to serve /api/pending (already in server.py). The answer comes
back via git commit, not DOM scraping.

This file is a placeholder for any future browser-automation code
(e.g. Chrome DevTools Protocol integration for headless operation,
or a Playwright-based sender). Currently unused — the existing Chrome
extension handles everything.

If you want to test without the extension, you can:
  1. POST /api/ask with a question + gitdrop target
  2. Manually paste the question into ChatGPT
  3. ChatGPT commits the answer via its GitHub connector
  4. The server detects the commit and marks the task completed
"""


def send_question_to_tab(channel, question):
    """Send a question to a ChatGPT tab.

    Stub — the Chrome extension handles this via /api/pending polling.
    This function exists as a future extension point for headless
    browser automation (CDP, Playwright, etc.).

    Returns True if the question was sent, False otherwise.
    """
    raise NotImplementedError(
        "Browser automation not implemented. "
        "Use the Chrome extension (polls /api/pending).")
