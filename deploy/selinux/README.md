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
2. **nginx reading the static directory.** `/var/www(/.*)?` already maps to
   `httpd_sys_content_t` in the shipped policy, so files created *in place*
   under `/var/www/agentlibrary/static` get the right label for free. They do
   **not** if you copied or moved them in from somewhere else (`cp -a`, `mv`,
   `tar` from `/root` or `/tmp` preserves the source label), which leaves
   `admin_home_t` or `user_tmp_t` and gets nginx `Permission denied` — a 403
   with an AVC in the audit log. `restorecon` fixes it; run it either way.
3. **The app writing uploads.** `/var/lib/agentlibrary/uploads` must be
   writable by the service. As an unconfined systemd service this normally
   works, but the label still matters if you later confine it.

## Commands

See the README, section 11, for the exact sequence. The short version:

```bash
# No fcontext rule for the static tree: /var/www(/.*)? is already
# httpd_sys_content_t in the base policy. Just enforce it.
sudo semanage fcontext -a -t httpd_var_run_t "/run/agentlibrary(/.*)?"
sudo semanage fcontext -a -t var_lib_t       "/var/lib/agentlibrary(/.*)?"
sudo semanage fcontext -a -t httpd_log_t     "/var/log/agentlibrary(/.*)?"
sudo restorecon -Rv /var/www/agentlibrary /var/lib/agentlibrary /var/log/agentlibrary
sudo setsebool -P httpd_can_network_connect 1
```

Deploying under `/var/www` labels the *whole* application directory
`httpd_sys_content_t`, including the code and the virtualenv. That is harmless:
gunicorn runs as an unconfined systemd service and may execute it, and nginx
only ever serves the paths its `location` blocks name — it has no `alias` for
anything but `/static/`. If you relocate the app outside `/var/www`, you must
add the rule back:

```bash
sudo semanage fcontext -a -t httpd_sys_content_t "<app-dir>/static(/.*)?"
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
