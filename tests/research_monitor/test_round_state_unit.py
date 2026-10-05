import json

from services.research_monitor import round_state


def test_update_round_complete_guards_cancelled_without_db(monkeypatch):
    """Non-cancel completion must not overwrite a manually cancelled round."""
    stored = {
        'status': 'cancelled',
        'summary_json': {'cancelled_reason': 'admin_cancelled'},
    }

    class FakeCursor:
        rowcount = 0

        def execute(self, sql, params):
            new_status = params[0]
            summary = json.loads(params[1])
            has_cancel_guard = (
                "status <> 'cancelled'" in sql
                or "status IS DISTINCT FROM 'cancelled'" in sql
                or "%s = 'cancelled'" in sql
            )
            if stored['status'] == 'cancelled' and new_status != 'cancelled' and has_cancel_guard:
                self.rowcount = 0
                return
            stored['status'] = new_status
            stored['summary_json'].update(summary)
            self.rowcount = 1

    class FakeConn:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(round_state, 'get_connection', lambda: FakeConn())

    result = round_state.update_round_complete(
        'round_cancelled',
        status='completed',
        summary={'raw_inserted': 1700},
    )

    assert result is False
    assert stored['status'] == 'cancelled'
    assert stored['summary_json'] == {'cancelled_reason': 'admin_cancelled'}
