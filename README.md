# salesforce-cert-mock-exam
Salesforce Admin Mock Exam


## Admin security
Admin pages are protected by `utils/access_control.py` and require an admin email.
Add this to Streamlit Secrets:

```toml
ADMIN_EMAILS = "your-admin-email@example.com"
```

You may use multiple admins:

```toml
ADMIN_EMAILS = "admin1@example.com, admin2@example.com"
```

Non-admin users are blocked at the top of each admin page even if they open the URL directly.
