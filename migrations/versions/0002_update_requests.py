"""Update requests and staged uploads.

Adds the v41 "Update an Agent" workflow:

* ``update_requests`` — a proposed change to a published asset, raised by
  anyone and applied only when an administrator accepts it.
* ``asset_files.update_request_id`` — files proposed on a request, which move
  onto the asset when the request is accepted.
* ``asset_files.asset_id`` becomes NULLABLE. The public submission forms let
  people attach files before the record exists, so an upload lands unbound
  ("staged") and is claimed on submit. ``flask prune-uploads`` sweeps any that
  are never claimed.

Apply with::

    FLASK_APP=wsgi.py flask db upgrade

Roll back with::

    FLASK_APP=wsgi.py flask db downgrade 0001_initial

Downgrading drops update_requests entirely, which destroys any unresolved
requests. Back up first.

Revision ID: 0002_update_requests
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002_update_requests'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


FK_UPDATE_REQUEST = "fk_asset_files_update_request_id"

# BigInteger on MySQL, INTEGER on SQLite — matching app/models.py so one
# migration serves the test suite and production.
_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "update_requests",
        sa.Column("id", _PK, nullable=False),
        sa.Column("asset_id", _PK, nullable=False),
        sa.Column("wgt_code", sa.String(length=16), nullable=False),
        sa.Column("asset_name", sa.String(length=200), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("proposed", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("requester_name", sa.String(length=160), nullable=False),
        sa.Column("requester_email", sa.String(length=254), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("submitted_ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_update_requests_asset_id", "update_requests", ["asset_id"])
    op.create_index("ix_update_requests_created_at", "update_requests", ["created_at"])
    op.create_index("ix_update_requests_status", "update_requests", ["status"])
    op.create_index("ix_update_requests_status_created", "update_requests",
                    ["status", "created_at"])
    op.create_index("ix_update_requests_wgt_code", "update_requests", ["wgt_code"])

    # batch_alter_table because SQLite cannot ALTER a column in place; on MySQL
    # this emits ordinary ALTER TABLE statements.
    with op.batch_alter_table("asset_files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("update_request_id", _PK, nullable=True))
        # Staged uploads exist before anything owns them.
        batch_op.alter_column("asset_id", existing_type=_PK,
                              existing_nullable=False, nullable=True)
        batch_op.create_index("ix_asset_files_update_request_id",
                              ["update_request_id"])
        # NAMED on purpose: autogenerate emits None here, which MySQL cannot
        # drop on downgrade.
        batch_op.create_foreign_key(FK_UPDATE_REQUEST, "update_requests",
                                    ["update_request_id"], ["id"],
                                    ondelete="CASCADE")


def downgrade():
    """Reverse the change.

    Destroys every update request, and any file attached to one. Take a backup
    before running this — see README section 15.
    """
    # Files proposed on an update request have no asset, so they cannot be
    # kept once the column goes; remove them before asset_id becomes NOT NULL
    # again, otherwise the ALTER fails on rows with a NULL asset_id.
    op.execute("DELETE FROM asset_files WHERE update_request_id IS NOT NULL "
               "OR asset_id IS NULL")

    with op.batch_alter_table("asset_files", schema=None) as batch_op:
        batch_op.drop_constraint(FK_UPDATE_REQUEST, type_="foreignkey")
        batch_op.drop_index("ix_asset_files_update_request_id")
        batch_op.drop_column("update_request_id")
        batch_op.alter_column("asset_id", existing_type=_PK,
                              existing_nullable=True, nullable=False)

    # Dropping the table drops its indexes; naming them separately here (as
    # autogenerate did) leaves the table behind if any drop fails.
    op.drop_table("update_requests")
