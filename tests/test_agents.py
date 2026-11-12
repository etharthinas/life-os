from app import agents

CODER = {"id": 1, "name": "coder"}
WRITER = {"id": 2, "name": "writer"}


def test_pick_agent():
    assert agents.pick_agent([CODER, WRITER], "@writer draft the post") == (WRITER, "draft the post")
    assert agents.pick_agent([CODER, WRITER], "fix the bug") == (CODER, "fix the bug")
    # an unknown @name is just text, for the first agent
    assert agents.pick_agent([CODER, WRITER], "@nobody hi") == (CODER, "@nobody hi")
    assert agents.pick_agent([], "hi") == (None, "hi")
