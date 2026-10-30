"""Outgoing email for verification codes, through any SMTP account (Gmail/Workspace, Microsoft 365, Brevo...).

Sending runs on a small pool of background threads that keep their SMTP connection open, so a burst of
students registering at once neither blocks the web threads nor opens one connection per message.
"""
import concurrent.futures
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

log = logging.getLogger('arena.mail')


class SmtpMailer:
    def __init__(self, host, port, user, password, sender, security='starttls', name='OTTv2 Arena', workers=2,
                 timeout=20):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender, self.name, self.security, self.timeout = sender, name, security, timeout
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mail')
        self.local = threading.local()

    def connect(self):
        context = ssl.create_default_context()
        if self.security == 'ssl':
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.security == 'starttls':
                smtp.starttls(context=context)
        if self.user:
            smtp.login(self.user, self.password)
        return smtp

    def deliver(self, message):
        # A kept-open connection may have been dropped by the server while idle: reconnect once and retry.
        for attempt in (1, 2):
            smtp = getattr(self.local, 'smtp', None)
            try:
                if smtp is None:
                    smtp = self.local.smtp = self.connect()
                smtp.send_message(message)
                return True
            except smtplib.SMTPRecipientsRefused:
                raise                                   # a bad address: the connection is fine, retrying won't help
            except OSError:                             # includes every other smtplib error
                self.local.smtp = None
                try:
                    smtp and smtp.close()
                except OSError:
                    pass
                if attempt == 2:
                    raise

    def send(self, to, subject, body):
        message = EmailMessage()
        message['From'] = formataddr((self.name, self.sender))
        message['To'] = to
        message['Subject'] = subject
        message['Message-ID'] = make_msgid(domain=self.sender.rsplit('@', 1)[-1])
        message.set_content(body)
        future = self.pool.submit(self.deliver, message)
        future.add_done_callback(lambda f: f.exception() and log.warning('Email to %s failed: %s', to, f.exception()))
        return future


class ConsoleMailer:
    """Local development: prints each message to the server log instead of sending it."""

    def send(self, to, subject, body):
        log.warning('EMAIL to %s | %s\n%s', to, subject, body)
        future = concurrent.futures.Future()
        future.set_result(True)
        return future


def mailer_from_env(local):
    host = os.getenv('ARENA_SMTP_HOST', '').strip()
    if not host:
        return None
    if host == 'console':
        if not local:
            raise RuntimeError('ARENA_SMTP_HOST=console only prints codes to the log; use a real SMTP server in production.')
        return ConsoleMailer()
    port = int(os.getenv('ARENA_SMTP_PORT', '587'))
    user = os.getenv('ARENA_SMTP_USER', '').strip()
    sender = os.getenv('ARENA_SMTP_FROM', '').strip() or user
    if '@' not in sender:
        raise RuntimeError('Set ARENA_SMTP_FROM (or ARENA_SMTP_USER) to the sending email address.')
    security = os.getenv('ARENA_SMTP_SECURITY', 'ssl' if port == 465 else 'starttls')
    if security not in ('starttls', 'ssl', 'none'):
        raise RuntimeError('ARENA_SMTP_SECURITY must be starttls, ssl or none.')
    return SmtpMailer(host, port, user, os.getenv('ARENA_SMTP_PASSWORD', ''), sender, security,
                      os.getenv('ARENA_SMTP_NAME', 'OTTv2 Arena'), int(os.getenv('ARENA_SMTP_WORKERS', '2')))
