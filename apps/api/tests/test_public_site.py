"""`GET /api/site` (docs/api.md): the support address and operator name the public policy
pages print, from the deployment's settings, with no session."""


async def test_the_site_answers_what_env_sets_and_null_otherwise(make_client) -> None:
    bare = make_client()
    unset = await bare.get("/api/site")
    assert unset.status_code == 200, unset.text
    assert unset.json() == {"support_email": None, "operator_name": None}

    client = make_client(support_email="help@example.com", operator_name="Example Co.")
    answer = await client.get("/api/site")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"support_email": "help@example.com", "operator_name": "Example Co."}
