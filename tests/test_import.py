def test_package_imports():
    import orcaslicer_mcp
    assert isinstance(orcaslicer_mcp.__version__, str)
