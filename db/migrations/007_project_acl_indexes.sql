-- Server-side project access uses SSO user/org context and these membership
-- relations.  Indexes keep the authorization check bounded as projects grow.
CREATE INDEX IF NOT EXISTS project_members_user_project_idx
    ON platform.project_members (user_id, project_id);
