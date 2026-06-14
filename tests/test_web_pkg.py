def test_flask_and_socketio_importable():
    import flask
    import flask_socketio
    import quietroom.web
    assert flask is not None and flask_socketio is not None
