# Simple & Clean Telegram Bot UI Plan

This plan strips out all clutter and keeps the Telegram Bot minimal, fast, and easy to understand. Everything happens inside **one clean bot message** that updates in place instead of spamming the chat.

## User Review & Key Changes

> [!IMPORTANT]
> Simplified based on your feedback — no complicated multi-step wizards or extra menus:

- **1. Simple Home Menu**: Shows your Workspace, Group status, and Attached IG in 3 short lines, with just 4 main buttons (`📸 Accounts`, `➕ Add IG`, `📊 Status`, `🔗 Group`) plus `🚪 Sign Out`.
- **2. Simple Login (Just Username → Password/Cookie)**:
  - Tap `➕ Add IG` → pick `Password` or `Cookie`.
  - Bot asks: *"Send Instagram username"* → you reply.
  - Bot asks: *"Send password (or cookie)"* → you reply, bot deletes your password message for privacy, and logs in immediately.
- **3. Clean Account View**: Tap `📸 Accounts` → tap any `@username` to see its status and simple buttons: `📌 Attach to Bridge`, `⚡ Realtime ON/OFF`, `🔔 FBNS ON/OFF`, `📥 Session File`, `🗑 Logout/Remove`, and `⬅️ Back`.
- **4. One Message (No Chat Spam)**: Clicking buttons updates the same message in-place instead of sending new messages every time.

---

## 1. Overview & Core Concept

- **What It Does**: Lets you sign into your workspace, add Instagram accounts, pick which Instagram account is attached to the Telegram group bridge, and toggle Realtime/FBNS from one clean menu.
- **Key Value**: Fast, minimal, and self-explanatory—no walls of text and no messy button grids.

---

## 2. User Experience & Menu Screens

### Screen 1: Sign In / Register (When starting the bot)
- **Text**:
  > **Hyper Insta Bridge**
  > Sign in to your workspace or create a new one.
- **Buttons**:
  - `[ 🔑 Login ]` `[ 🆕 Register ]`
- **How it works**: Bot asks for your username, then your password (with a `[ ❌ Cancel ]` button), deletes your password message, and opens the Home Menu.

### Screen 2: Home Menu (`/start`)
- **Text**:
  > **Hyper Insta Bridge**
  > 👤 **Workspace:** `rex`
  > 💬 **Group:** Linked (`-100...`)
  > 📸 **Bridged IG:** `@username` (🟢 Online)
- **Buttons**:
  - `[ 📸 Accounts (1) ]` `[ ➕ Add IG ]`
  - `[ 📊 Status ]` `[ 🔗 Link Group ]`
  - `[ 🔄 Refresh ]` `[ 🚪 Sign Out ]`

### Screen 3: Accounts List (`📸 Accounts`)
- **Text**:
  > **Instagram Accounts**
  > Tap an account to manage it or attach it to the bridge:
- **Buttons**:
  - `[ ✅ @account1 (🟢) ]`
  - `[ ⚪ @account2 (🟠) ]`
  - `[ ➕ Add IG ]` `[ ⬅️ Back ]`

### Screen 4: Single Account Card (When you tap an `@account`)
- **Text**:
  > **@account1**
  > Status: 🟢 Logged In (`password`)
  > Realtime: **ON** · FBNS: **ON**
  > Bridged Topics: **4**
- **Buttons**:
  - `[ 📌 Attach to Bridge ]` *(or `✅ Attached to Bridge`)*
  - `[ ⚡ Realtime: ON ]` `[ 🔔 FBNS: ON ]`
  - `[ 🔄 Relogin ]` `[ 📥 Session .json ]`
  - `[ 🗑 Logout IG ]` `[ ⬅️ Back ]`

### Screen 5: Add Instagram Account (`➕ Add IG`)
- **Buttons**:
  - `[ 🔐 Password Login ]` `[ 🍪 Cookie Login ]`
  - `[ ⬅️ Back ]`
  *(You can also drop a `session.json` file into the chat at any time to import it.)*

---

## 3. Slash Commands (Telegram `/` Menu)

Registered automatically in Telegram's command button:
- `/start` — Open main menu
- `/accounts` — View & switch Instagram accounts
- `/status` — Show connection status
- `/link` — Link your Telegram Forum Group (run inside the group)
- `/logout` — Sign out of workspace

---

## 4. Technical Flow

```
┌──────────────────────────────────────────────┐
│                 Home Menu                    │
│  [ 📸 Accounts ]          [ ➕ Add IG ]      │
│  [ 📊 Status ]            [ 🔗 Link Group ]  │
│  [ 🔄 Refresh ]           [ 🚪 Sign Out ]    │
└─────────┬────────────────────────┬───────────┘
          │                        │
          ▼                        ▼
┌─────────────────────┐  ┌─────────────────────┐
│   Accounts List     │  │   Add IG Account    │
│  Tap @username ───┐ │  │  1. Send username   │
└───────────────────┼─┘  │  2. Send password   │
                    ▼    │     or cookie       │
┌─────────────────────┐  └─────────────────────┘
│    Account Card     │
│  • Attach to Bridge │
│  • Realtime / FBNS  │
│  • Relogin / Export │
│  • Logout / Back    │
└─────────────────────┘
```
