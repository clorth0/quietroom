def test_package_imports():
    import quietroom
    import quietroom.engine
    assert quietroom.engine is not None
