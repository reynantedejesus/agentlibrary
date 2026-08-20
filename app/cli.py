"""Flask CLI commands.

    flask create-admin --email admin@example.com     # interactive password prompt
    flask create-user  --email a@b.c --role reviewer
    flask set-role     --email a@b.c --role admin
    flask reset-password --email a@b.c
    flask seed-config                                 # reference data ONLY
    flask import-legacy dump.json [--dry-run]         # optional localStorage import
    flask show-status                                 # quick health/inventory check
    flask prune-activity --days 730

No password is ever hard-coded. ``create-admin`` prompts on a TTY; in an
automated deployment it reads ``ADMIN_INITIAL_PASSWORD`` from the environment
(supplied by systemd's ``EnvironmentFile=`` or a secrets manager) and refuses
to run if neither is available.

Connects to: registered onto the app in ``app/__init__.py::_register_cli``.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from typing import Optional

import click
from flask.cli import with_appcontext

from app import reference
from app.extensions import db


def _prompt_password(env_var: str = "ADMIN_INITIAL_PASSWORD") -> str:
    """Interactive prompt, or a deployment-supplied environment variable."""
    from flask import current_app
    from app.validators import password_strength_problem

    minimum = current_app.config.get("MIN_PASSWORD_LENGTH", 12)
    from_env = os.environ.get(env_var)
    if from_env:
        problem = password_strength_problem(from_env, minimum)
        if problem:
            raise click.ClickException("{0} (from {1})".format(problem, env_var))
        click.echo("Using the password supplied in {0}.".format(env_var))
        return from_env

    if not sys.stdin.isatty():
        raise click.ClickException(
            "No TTY for an interactive prompt and {0} is not set. "
            "Supply the password through your deployment secret store.".format(env_var))

    for _ in range(3):
        first = getpass.getpass("New password (min {0} chars): ".format(minimum))
        problem = password_strength_problem(first, minimum)
        if problem:
            click.echo("  " + problem, err=True)
            continue
        second = getpass.getpass("Confirm password: ")
        if first != second:
            click.echo("  Passwords did not match.", err=True)
            continue
        return first
    raise click.ClickException("Password not set after three attempts.")


def _upsert_user(email: str, full_name: str, role: str, force: bool):
    from app import audit
    from app.models import User

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise click.ClickException("A valid email address is required.")
    if role not in reference.ROLES:
        raise click.ClickException("Role must be one of: {0}".format(", ".join(reference.ROLES)))

    existing = User.query.filter_by(email=email).first()
    if existing and not force:
        raise click.ClickException(
            "{0} already exists (role={1}). Re-run with --force to reset it.".format(
                email, existing.role))

    password = _prompt_password()
    user = existing or User(email=email)
    user.full_name = full_name or user.full_name or email.split("@")[0].replace(".", " ").title()
    user.role = role
    user.is_active_flag = True
    user.failed_login_count = 0
    user.locked_until = None
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit.record(audit.USER_CREATE if not existing else audit.USER_UPDATE, user,
                 {"role": role, "via": "cli"}, actor_email="cli")
    db.session.commit()
    return user


def register_cli(app) -> None:

    @app.cli.command("create-admin")
    @click.option("--email", required=True, help="Email address of the administrator.")
    @click.option("--name", "full_name", default="", help="Display name.")
    @click.option("--force", is_flag=True, help="Reset the password if the account exists.")
    @with_appcontext
    def create_admin(email, full_name, force):
        """Create (or reset) the initial administrator account."""
        user = _upsert_user(email, full_name, reference.ROLE_ADMIN, force)
        click.echo("Administrator ready: {0}".format(user.email))

    @app.cli.command("create-user")
    @click.option("--email", required=True)
    @click.option("--name", "full_name", default="")
    @click.option("--role", default=reference.ROLE_USER,
                  type=click.Choice(reference.ROLES))
    @click.option("--force", is_flag=True)
    @with_appcontext
    def create_user(email, full_name, role, force):
        """Create a user, reviewer or admin account."""
        user = _upsert_user(email, full_name, role, force)
        click.echo("Created {0} with role {1}".format(user.email, user.role))

    @app.cli.command("set-role")
    @click.option("--email", required=True)
    @click.option("--role", required=True, type=click.Choice(reference.ROLES))
    @with_appcontext
    def set_role(email, role):
        """Change an existing account's role."""
        from app import audit
        from app.models import User

        user = User.query.filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException("No such user: {0}".format(email))
        previous, user.role = user.role, role
        audit.record(audit.USER_UPDATE, user,
                     {"from": previous, "to": role, "via": "cli"}, actor_email="cli")
        db.session.commit()
        click.echo("{0}: {1} -> {2}".format(user.email, previous, role))

    @app.cli.command("reset-password")
    @click.option("--email", required=True)
    @with_appcontext
    def reset_password(email):
        """Set a new password for an existing account."""
        from app import audit
        from app.models import User

        user = User.query.filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException("No such user: {0}".format(email))
        user.set_password(_prompt_password())
        user.failed_login_count = 0
        user.locked_until = None
        audit.record(audit.PASSWORD_CHANGE, user, {"via": "cli"}, actor_email="cli")
        db.session.commit()
        click.echo("Password updated for {0}".format(user.email))

    @app.cli.command("seed-config")
    @with_appcontext
    def seed_config():
        """Seed reference/configuration data only.

        This command deliberately cannot create assets. There is no demo-data
        code path in the application at all.
        """
        from app import wgt
        from app.models import ConfigSetting

        defaults = reference.default_reference()
        written = 0
        for key in reference.SEEDABLE_KEYS:
            if db.session.get(ConfigSetting, key) is None:
                db.session.add(ConfigSetting(key=key, value=defaults[key]))
                written += 1
        db.session.commit()
        counters = wgt.ensure_counter_rows()
        click.echo("Reference rows written: {0}".format(written))
        click.echo("WGT counter rows created: {0}".format(counters))
        click.echo("Assets in database: {0} (seeding never creates assets)".format(
            wgt.total_assets()))

    @app.cli.command("show-status")
    @with_appcontext
    def show_status():
        """Print a short inventory — handy after a deploy."""
        from sqlalchemy import func
        from app import wgt
        from app.models import ActivityLog, Asset, AssetFile, User

        click.echo("Database : {0}".format(
            app.config["SQLALCHEMY_DATABASE_URI"].split("@")[-1]))
        click.echo("Upload dir: {0}".format(app.config["UPLOAD_DIR"]))
        click.echo("Users     : {0}".format(db.session.query(func.count(User.id)).scalar()))
        click.echo("Assets    : {0}".format(db.session.query(func.count(Asset.id)).scalar()))
        for status in reference.STATUSES:
            count = db.session.query(func.count(Asset.id)).filter(
                Asset.status == status).scalar()
            click.echo("  {0:<16}{1}".format(status, count))
        click.echo("Files     : {0}".format(db.session.query(func.count(AssetFile.id)).scalar()))
        click.echo("Activity  : {0}".format(db.session.query(func.count(ActivityLog.id)).scalar()))
        click.echo("WGT counters: {0}".format(wgt.counter_state()))

    @app.cli.command("prune-activity")
    @click.option("--days", default=730, show_default=True,
                  help="Delete activity_log rows older than this.")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    @with_appcontext
    def prune_activity(days, yes):
        """Trim the audit trail to a retention window."""
        import datetime as dt
        from app.models import ActivityLog

        cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
        query = ActivityLog.query.filter(ActivityLog.ts < cutoff)
        count = query.count()
        if not count:
            click.echo("Nothing older than {0} days.".format(days))
            return
        if not yes and not click.confirm("Delete {0} activity rows?".format(count)):
            return
        query.delete(synchronize_session=False)
        db.session.commit()
        click.echo("Deleted {0} rows.".format(count))

    @app.cli.command("import-legacy")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--dry-run", is_flag=True, help="Report what would be imported.")
    @click.option("--owner-email", default=None,
                  help="Fallback account to own assets whose owner has no account.")
    @with_appcontext
    def import_legacy(path, dry_run, owner_email):
        """Import a localStorage dump exported from the HTML prototype.

        Expects the JSON that was stored under ``wgt_agent_library_v1``:
        ``{"schemaVersion": 5, "assets": [...], "activity": [...]}``.
        Existing WGT codes are preserved and the counters are advanced past
        them. This is an optional one-off migration aid, not a seeding path.
        """
        import datetime as dt
        from app import wgt
        from app.models import Asset, AssetVersion, User

        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, str):
            raw = json.loads(raw)
        records = raw.get("assets") or []
        click.echo("Found {0} asset records in {1}".format(len(records), path))

        fallback: Optional[User] = None
        if owner_email:
            fallback = User.query.filter_by(email=owner_email.strip().lower()).first()
            if fallback is None:
                raise click.ClickException("No such user: {0}".format(owner_email))

        imported = skipped = 0
        for record in records:
            code = (record.get("wgtCode") or "").strip()
            name = (record.get("name") or "").strip()
            if not code or not name:
                skipped += 1
                continue
            if Asset.query.filter_by(wgt_code=code).first():
                click.echo("  skip {0} (already present)".format(code))
                skipped += 1
                continue
            if dry_run:
                click.echo("  would import {0} — {1}".format(code, name))
                imported += 1
                continue

            owner = None
            email = (record.get("ownerEmail") or "").strip().lower()
            if email:
                owner = User.query.filter_by(email=email).first()
            owner = owner or fallback

            def _date(value, default=None):
                try:
                    return dt.datetime.strptime(value, "%Y-%m-%d")
                except (TypeError, ValueError):
                    return default or dt.datetime.utcnow()

            asset = Asset(
                wgt_code=code,
                name=name,
                asset_type=record.get("type") or "other",
                platform=record.get("platform") or reference.platform_for_type(
                    record.get("type") or "other"),
                department=record.get("department") or "Other",
                status=record.get("status") or reference.STATUS_PENDING,
                description=record.get("description") or "",
                use_case=record.get("useCase") or "",
                problem_solved=record.get("problemSolved"),
                input_requirements=record.get("inputRequirements"),
                expected_output=record.get("expectedOutput"),
                example_use=record.get("exampleUse"),
                owner_id=getattr(owner, "id", None),
                creator_id=getattr(owner, "id", None),
                owner_name=record.get("owner") or "",
                owner_email=email,
                creator_name=record.get("creator") or "",
                submitted_by_name=record.get("submittedBy") or "",
                access_level=record.get("accessLevel") or "Company-wide",
                sensitivity=record.get("sensitivity") or "Standard Internal",
                review_frequency=record.get("reviewFrequency") or "Every 6 months",
                current_version=record.get("currentVersion") or "1.0",
                direct_url=record.get("directUrl") or "",
                featured=bool(record.get("featured")),
                configuration=record.get("configuration") or {},
                additional_departments=record.get("additionalDepartments") or [],
                created_at=_date(record.get("createdDate")),
                last_updated_at=_date(record.get("lastUpdated")),
                submitted_at=_date(record.get("submissionDate")),
                published_at=_date(record.get("publishedDate"), None)
                if record.get("publishedDate") else None,
                last_verified_at=_date(record.get("lastVerified"), None)
                if record.get("lastVerified") else None,
            )
            asset.set_tags(record.get("tags") or [])
            asset.next_review_date = asset.compute_next_review()
            db.session.add(asset)
            db.session.flush()

            for entry in (record.get("versions") or []):
                db.session.add(AssetVersion(
                    asset_id=asset.id,
                    version_number=str(entry.get("versionNumber") or "1.0")[:16],
                    summary=entry.get("summary") or "",
                    updated_by_name=entry.get("updatedBy") or "",
                    is_current=bool(entry.get("isCurrent")),
                    created_at=_date(entry.get("date")),
                ))
            imported += 1
            click.echo("  imported {0} — {1}".format(code, name))

        if dry_run:
            db.session.rollback()
            click.echo("Dry run: {0} would import, {1} skipped.".format(imported, skipped))
            return
        db.session.commit()
        for department in reference.get_reference("departments"):
            wgt._resync_counter(department)
        click.echo("Imported {0}, skipped {1}. WGT counters resynced.".format(imported, skipped))
