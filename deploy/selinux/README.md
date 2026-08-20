# SELinux notes

The application is expected to run with **SELinux in enforcing mode**. Nothing
here requires permissive mode; if you find yourself reaching for
`setenforce 0`, stop and read the denial instead — the fix is almost always a
file context or a boolean.

## What SELinux actually blocks here

Three things, in practice:

1. **nginx connecting to the gunicorn Unix socket.** nginx runs as `httpd_t`.
   It may only connect to sockets it is allowed to reach. Fixed with the
   `httpd_can_network_connect` boolean plus a correct label on
   `/run/agentlibrary`.
2. **nginx reading the static directory.** `/opt/agentlibrary/static` is not a
   default web root, so its files carry `usr_t` rather than `httpd_sys_content_t`
   and nginx gets `Permission denied` (a 403 with an AVC in the audit log).
3. **The app writing uploads.** `/var/lib/agentlibrary/uploads` must be
   writable by the service. As an unconfined systemd service this normally
   works, but the label still matters if you later confine it.

## Commands

See the README, section 7, for the exact sequence. The short version:

```bash
sudo semanage fcontext -a -t httpd_sys_content_t "/opt/agentlibrary/static(/.*)?"
sudo semanage fcontext -a -t httpd_var_run_t     "/run/agentlibrary(/.*)?"
sudo semanage fcontext -a -t var_lib_t           "/var/lib/agentlibrary(/.*)?"
sudo semanage fcontext -a -t httpd_log_t         "/var/log/agentlibrary(/.*)?"
sudo restorecon -Rv /opt/agentlibrary/static /var/lib/agentlibrary /var/log/agentlibrary
sudo setsebool -P httpd_can_network_connect 1
```

`/run` is a tmpfs recreated at boot, so the `semanage fcontext` rule for
`/run/agentlibrary` is what makes the label survive a reboot — `restorecon`
alone would not.

## Reading denials

```bash
sudo ausearch -m AVC -ts recent
sudo ausearch -m AVC -ts recent | audit2why
sudo journalctl -t setroubleshoot -n 50
```

`audit2why` explains *why* something was denied and names the boolean that
would allow it, which is nearly always a better fix than a custom module.
