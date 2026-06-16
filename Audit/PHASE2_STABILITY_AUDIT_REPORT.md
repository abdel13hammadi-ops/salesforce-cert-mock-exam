# Phase 2 Stability Audit Report

## Scope
Reviewed and patched the uploaded Streamlit + Supabase project for the current Phase 2 problems:

- Refresh logout
- Admin pages visible before login
- Account login instability
- My Progress not seeing existing attempts
- Duplicate/stale root pages
- Missing Streamlit sidebar config

## Main fixes

### 1. Central auth/access helper
Rebuilt `utils/access_control.py` as the central helper for:

- Signed session restore
- Current user email
- Subscription status
- Premium access checks
- Admin unlock
- Custom sidebar navigation
- Hiding Streamlit native multipage sidebar

### 2. Refresh persistence
Implemented a signed URL session token using `fr_session` query parameter.

This is more reliable on Streamlit Cloud than the previous browser-cookie/localStorage component attempts.

The token is signed with `COOKIE_PASSWORD` and expires after 30 days. It contains no password or service-role key.

### 3. Account page rebuilt
Rebuilt `pages/Account.py` to:

- Use Supabase Auth for login/signup
- Preserve existing `app_users.subscription_status`
- Persist login to the signed session token
- Add admin unlock flow directly on Account page
- Cleanly logout and clear persisted login

### 4. Admin sidebar visibility
Added `.streamlit/config.toml`:

```toml
[client]
showSidebarNavigation = false
```

Also added CSS fallback to hide Streamlit's native sidebar nav.

### 5. Admin page protection
Admin pages now call `require_admin()` immediately after page config/navigation setup:

- `pages/Admin_Import.py`
- `pages/Admin_Question_Review.py`
- `pages/Admin_Support_Tickets.py`

### 6. My Progress fixes
Updated `pages/My_Progress.py` so:

- It uses restored logged-in email
- It does not depend on `created_at`
- It does not filter out old attempts by strict `language_code`
- It falls back to active certifications if `user_certification_access` rows are missing

### 7. Removed unsafe/stale files
Removed:

- `.streamlit/secrets.toml`
- root duplicate `Practice_By_Category.py`
- root duplicate `Weak_Areas_Practice.py`
- `__pycache__`

## Files changed

- `utils/access_control.py`
- `pages/Account.py`
- `pages/My_Progress.py`
- `pages/Practice_By_Category.py`
- `pages/Weak_Areas_Practice.py`
- `pages/Support.py`
- `pages/Admin_Import.py`
- `pages/Admin_Question_Review.py`
- `pages/Admin_Support_Tickets.py`
- `app.py`
- `.streamlit/config.toml`
- `.streamlit/secrets.toml.example`
- `.gitignore`

## Deployment notes

Do not upload ZIP into GitHub. Extract and replace actual files.

Required Streamlit secrets:

```toml
SUPABASE_URL = "..."
SUPABASE_ANON_KEY = "..."
SUPABASE_SERVICE_ROLE_KEY = "..."
ADMIN_EMAILS = "admin@example.com"
ADMIN_PASSWORD = "your-admin-password"
COOKIE_PASSWORD = "one-long-stable-random-value"
ALLOW_TRIAL_AS_PAID = "false"
```

## Remaining honest risks

- This is still Streamlit, not a production-grade auth framework.
- Signed URL session is reliable for refresh, but weaker than true browser Supabase Auth cookies.
- Service role is still used server-side in several pages because this app architecture is Streamlit-server based.
- Long-term best architecture is Next.js + Supabase Auth + RLS, not Streamlit.

## Test checklist

1. Deploy to Streamlit Cloud.
2. Reboot app.
3. Open app logged out.
4. Confirm admin pages are not visible.
5. Go to Account.
6. Login.
7. Confirm signed-in email appears.
8. Hard refresh.
9. Confirm user stays signed in.
10. Open My Progress as paid user.
11. Confirm existing attempts show.
12. Logout.
13. Hard refresh.
14. Confirm user stays logged out.
15. Login as admin email.
16. Confirm admin pages are hidden before unlock.
17. Unlock admin from Account.
18. Confirm admin pages appear and open.
