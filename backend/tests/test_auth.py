from ..app import load_user

def test_health(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.get_json()["message"] == "Flask backend is running."

def test_register_login_logout(client, app):
    rv = client.post("/register", json={
        "username": "alice",
        "email": "alice@mail.com",
        "password": "pwd123",
        "grade": 31
    })
    assert rv.status_code in (200, 201)

    rv = client.post("/login", json={
        "username": "alice",
        "password": "pwd123"
    })
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["message"] == "Login successful"
    assert data["user"]["username"] == "alice"

    with app.app_context():
        uid = load_user(rv.get_json()["user"]["id"])
        assert uid.username == "alice"

    # get session status
    rv = client.get("/session-status")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["authenticated"] is True
    assert data["user"]["username"] == "alice"

    # request reset
    rv = client.post("/request-reset", json={
        "email": "alice@mail.com"
    })
    assert rv.status_code == 200
    
    # logout
    rv = client.post("/logout")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["message"] == "Logged out"


def test_google_oauth2_login(client):
    rv = client.get("/auth/google/login")
    assert rv.status_code == 302  # Redirect to Google OAuth
