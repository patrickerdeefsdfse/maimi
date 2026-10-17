"""Compare the other four 016 tables using the original catalog projection."""
def verify(cur,expected):
    if (expected.get('schema')!='r43-preserved-project-tables-v1'
        or expected.get('tables')!=['project_scopes','project_session_state','project_jobs','project_invocation_revocations']
        or set(expected.get('queries',{}))!={'tables','columns','constraints','indexes','triggers'}):
        raise RuntimeError('R43_PRESERVED_SCHEMA_EXPECTATION_INVALID')
    actual={}
    for name,query in expected['queries'].items():
        cur.execute(query,(expected['tables'],))
        actual[name]=[list(row.values()) for row in cur.fetchall()]
    if actual!=expected['catalog']:raise RuntimeError('R43_PRESERVED_PROJECT_SCHEMA_CHANGED')
    return True
