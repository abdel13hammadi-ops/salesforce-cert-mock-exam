# salesforce-cert-mock-exam
Salesforce Admin Mock Exam

## Admin Login Security

This project uses a custom admin login flow.

Add these values in Streamlit Cloud → App → Settings → Secrets:

```toml
ADMIN_EMAILS = "your-admin-email@example.com"
ADMIN_PASSWORD = "your-strong-admin-password"
```

Normal users see only user pages plus an Admin login page. Admin-only pages remain hidden until an authorized admin email logs in from Account and unlocks Admin with the password. Direct access to admin pages is blocked by `require_admin()`.
