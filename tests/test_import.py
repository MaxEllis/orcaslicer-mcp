def test_package_imports():
    import orcaslicer_mcp
    assert isinstance(orcaslicer_mcp.__version__, str)


def test_server_has_no_unused_top_level_imports():
    # An import left behind when its last use went away (math after the remembered-age rewrite,
    # sqlite3 after save_gcode started catching every recording error) is dead weight for readers.
    import ast
    from pathlib import Path
    import orcaslicer_mcp.server as srv
    tree = ast.parse(Path(srv.__file__).read_text())
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update({(a.asname or a.name).split(".")[0]: node.lineno for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            imported.update({(a.asname or a.name): node.lineno for a in node.names})
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert {name: line for name, line in imported.items() if name not in used} == {}
