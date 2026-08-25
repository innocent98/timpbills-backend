def render_email_otp(*, code: str) -> tuple[str, str]:
    """Return (html, text) for a simple branded OTP email."""
    html = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #F0F1FB; padding: 40px 20px; margin: 0;">
  <table role="presentation" cellpadding="0" cellspacing="0" style="max-width: 520px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; overflow: hidden;">
    <tr>
      <td style="background: #4F46E5; padding: 28px 32px;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 22px; font-weight: 700;">Timpbills</h1>
      </td>
    </tr>
    <tr>
      <td style="padding: 40px 32px;">
        <h2 style="color: #0F172A; font-size: 24px; font-weight: 700; margin: 0 0 12px;">Verify your email</h2>
        <p style="color: #475569; font-size: 15px; line-height: 1.6; margin: 0 0 28px;">
          Use the 6-digit code below to finish setting up your Timpbills account.
        </p>
        <div style="background: #F0F1FB; border-radius: 12px; padding: 24px; text-align: center; margin-bottom: 28px;">
          <div style="color: #4F46E5; font-size: 36px; font-weight: 700; letter-spacing: 8px; font-family: ui-monospace, SFMono-Regular, monospace;">{code}</div>
        </div>
        <p style="color: #64748B; font-size: 13px; line-height: 1.6; margin: 0;">
          This code expires in 30 minutes. If you didn't request this, you can safely ignore this email.
        </p>
      </td>
    </tr>
    <tr>
      <td style="background: #F8FAFC; padding: 20px 32px; border-top: 1px solid #E2E8F0;">
        <p style="color: #94A3B8; font-size: 12px; margin: 0;">
          Timpbills &middot; Nigeria's fast, reliable bills &amp; travel app
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""

    text = f"""Timpbills \u2014 verify your email

Use this 6-digit code to finish setting up your account:

    {code}

The code expires in 30 minutes. If you didn't request this, ignore this email.

\u2014 Timpbills
"""
    return html, text
