-- The employee distribution feed, runner catalog, and admin lookup start with the employee.
CREATE INDEX IF NOT EXISTS enterprise_plugin_grants_by_employee
    ON platform.enterprise_plugin_grants(user_no, plugin_key, version)
    WHERE enabled;
