import os
from datetime import datetime
from uuid import uuid4

from flask import current_app, safe_join
from flask_login import current_user
from sqlalchemy import func, orm
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy_utils import aggregated, generic_repr

from ..auth import current_user_is_roadie
from ..db import postgres as db
from ..members.models import User
from ..mixins import Syncable


@generic_repr("id", "name")
class Project(db.Model, Syncable):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False, index=True)
    normalized_name = orm.column_property(func.normalize_pep426_name(name))
    description = db.Column(db.Text)
    html_url = db.Column(db.String(255))
    subscribers_count = db.Column(db.SmallInteger, default=0, nullable=False)
    stargazers_count = db.Column(db.SmallInteger, default=0, nullable=False)
    forks_count = db.Column(db.SmallInteger, default=0, nullable=False)
    open_issues_count = db.Column(db.SmallInteger, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    transfer_issue_url = db.Column(db.String(255))
    membership = db.relationship("ProjectMembership", backref="project", lazy="dynamic")

    credentials = db.relationship(
        "ProjectCredential", backref="project", lazy="dynamic"
    )
    uploads = db.relationship(
        "ProjectUpload",
        backref="project",
        lazy="dynamic",
        order_by=lambda: ProjectUpload.ordering.desc().nullslast(),
    )

    created_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    pushed_at = db.Column(db.DateTime, nullable=True)

    __tablename__ = "projects"
    __table_args__ = (
        db.Index("release_name_idx", "name"),
        db.Index("release_name_is_active_idx", "name", "is_active"),
    )

    def __str__(self):
        return self.name

    @aggregated("uploads", db.Column(db.SmallInteger))
    def uploads_count(self):
        return db.func.count("1")

    @property
    def current_user_is_member(self):
        if not current_user:
            return False
        elif not current_user.is_authenticated:
            return False
        elif current_user_is_roadie():
            return True
        else:
            return current_user.id in self.member_ids

    @property
    def member_ids(self):
        return [member.user.id for member in self.membership.all()]

    @property
    def leads(self):
        leads = self.membership.filter(
            ProjectMembership.is_lead.is_(True),
            ProjectMembership.user_id.in_(
                User.active_members().options(orm.load_only("id"))
            ),
        )
        return [member.user for member in leads]

    @property
    def pypi_json_url(self):
        return f"https://pypi.org/pypi/{self.normalized_name}/json"  # noqa


@generic_repr("id", "project_id", "is_active", "key")
class ProjectCredential(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    key = db.Column(UUID(as_uuid=True), default=uuid4)

    __tablename__ = "project_credentials"
    __table_args__ = (db.Index("release_key_is_active_idx", "key", "is_active"),)

    def __str__(self):
        return self.key.hex


@generic_repr("id", "user_id", "project_id", "is_lead")
class ProjectMembership(db.Model):
    id = db.Column("id", db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_lead = db.Column(db.Boolean, default=False, nullable=False, index=True)

    __tablename__ = "project_memberships"

    def __str__(self):
        return f"User: {self.user}, Project: {self.project}"


@generic_repr("id", "project_id", "filename")
class ProjectUpload(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    version = db.Column(db.Text, index=True)
    path = db.Column(db.Text, unique=True, index=True)
    filename = db.Column(db.Text, unique=True, index=True)
    signaturename = orm.column_property(filename + ".asc")

    size = db.Column(db.Integer)
    md5_digest = db.Column(db.Text, unique=True, nullable=False)
    sha256_digest = db.Column(db.Text, unique=True, nullable=False)
    blake2_256_digest = db.Column(db.Text, unique=True, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    released_at = db.Column(db.DateTime, nullable=True)
    notified_at = db.Column(db.DateTime, nullable=True, index=True)
    form_data = db.Column(JSONB)
    user_agent = db.Column(db.Text)
    remote_addr = db.Column(db.Text)
    ordering = db.Column(db.Integer, default=0)

    __tablename__ = "project_uploads"
    __table_args__ = (
        db.CheckConstraint("sha256_digest ~* '^[A-F0-9]{64}$'"),
        db.CheckConstraint("blake2_256_digest ~* '^[A-F0-9]{64}$'"),
        db.Index("project_uploads_project_version", "project_id", "version"),
    )

    @property
    def full_path(self):
        # build storage path, e.g.
        # /app/uploads/acme/2coffee12345678123123123123123123
        return safe_join(current_app.config["UPLOAD_ROOT"], self.path)

    @property
    def signature_path(self):
        return self.full_path + ".asc"

    def __str__(self):
        return self.filename


@db.event.listens_for(ProjectUpload, "after_delete")
def delete_upload_file(mapper, connection, target):
    # When a model with a timestamp is updated; force update the updated
    # timestamp.
    os.remove(target.full_path)
    if os.path.exists(target.signature_path):
        os.remove(target.signature_path)
