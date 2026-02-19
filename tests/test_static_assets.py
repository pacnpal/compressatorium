"""Smoke tests for static asset caching and module serving."""

import importlib

from fastapi.testclient import TestClient


def _create_static_tree(tmp_path):
    static_dir = tmp_path / 'static'
    js_dir = static_dir / 'js'
    nested_js_dir = js_dir / 'app'
    features_dir = nested_js_dir / 'features' / 'igir'
    nested_js_dir.mkdir(parents=True)
    features_dir.mkdir(parents=True)

    (static_dir / 'index.html').write_text(
        '<!DOCTYPE html><html><body><div id="app"></div></body></html>',
        encoding='utf-8',
    )
    (js_dir / 'app.js').write_text(
        "import './app/AppRoot.js';\n",
        encoding='utf-8',
    )
    (nested_js_dir / 'AppRoot.js').write_text(
        'export const APP_ROOT_SENTINEL = true;\n',
        encoding='utf-8',
    )
    (features_dir / 'IgirView.js').write_text(
        'export const IGIR_VIEW_SENTINEL = true;\n',
        encoding='utf-8',
    )

    return static_dir


def _load_main_with_static_dir(monkeypatch, static_dir):
    monkeypatch.setenv('STATIC_DIR', str(static_dir))

    import app.main as main_module

    module = importlib.reload(main_module)

    async def _noop_process_queue():
        return None

    monkeypatch.setattr(module.job_manager, 'process_queue', _noop_process_queue)
    return module


def test_root_index_has_no_store_cache_header(tmp_path, monkeypatch):
    static_dir = _create_static_tree(tmp_path)
    main_module = _load_main_with_static_dir(monkeypatch, static_dir)

    with TestClient(main_module.app) as client:
        response = client.get('/')

    assert response.status_code == 200
    assert response.headers.get('cache-control') == 'no-store'


def test_static_js_entry_has_module_revalidation_header(tmp_path, monkeypatch):
    static_dir = _create_static_tree(tmp_path)
    main_module = _load_main_with_static_dir(monkeypatch, static_dir)

    with TestClient(main_module.app) as client:
        response = client.get('/static/js/app.js')

    assert response.status_code == 200
    assert response.headers.get('cache-control') == 'no-cache, must-revalidate'
    assert 'javascript' in response.headers.get('content-type', '')


def test_static_split_module_path_is_served_with_js_cache_policy(tmp_path, monkeypatch):
    static_dir = _create_static_tree(tmp_path)
    main_module = _load_main_with_static_dir(monkeypatch, static_dir)

    with TestClient(main_module.app) as client:
        response = client.get('/static/js/app/AppRoot.js')

    assert response.status_code == 200
    assert response.headers.get('cache-control') == 'no-cache, must-revalidate'
    assert 'javascript' in response.headers.get('content-type', '')


def test_static_deep_split_module_path_is_served_with_js_cache_policy(tmp_path, monkeypatch):
    static_dir = _create_static_tree(tmp_path)
    main_module = _load_main_with_static_dir(monkeypatch, static_dir)

    with TestClient(main_module.app) as client:
        response = client.get('/static/js/app/features/igir/IgirView.js')

    assert response.status_code == 200
    assert response.headers.get('cache-control') == 'no-cache, must-revalidate'
    assert 'javascript' in response.headers.get('content-type', '')
