#!/usr/bin/env node
import assert from 'node:assert/strict';

import {
  getFileExtension,
  get3dsProductPath,
  getDolphinProductPath,
  is3dsFile,
  is3dsSourceFile,
  is3dsVerifyFile,
} from '../static/js/app/utils/fileTypeUtils.js';
import { buildCompressionValue, cloneSelectionMap } from '../static/js/app/utils/stateUtils.js';
import {
  loadStoredConversionPresets,
  makeConversionPresetId,
} from '../static/js/app/utils/conversionPresetUtils.js';
import { CONVERSION_PRESETS_STORAGE_KEY } from '../static/js/app/constants/uiConstants.js';

function withMockLocalStorage(impl, fn) {
  const original = globalThis.localStorage;
  globalThis.localStorage = impl;
  try {
    fn();
  } finally {
    globalThis.localStorage = original;
  }
}

function run() {
  assert.equal(getFileExtension('/games/rom.CIA'), '.cia');
  assert.equal(getFileExtension('/games/noext'), '');
  assert.equal(getFileExtension('/games/.hidden'), '');

  assert.equal(is3dsSourceFile('/games/a.3ds'), true);
  assert.equal(is3dsVerifyFile('/games/a.z3ds'), true);
  assert.equal(is3dsFile('/games/a.zcia'), true);
  assert.equal(is3dsFile('/games/a.iso'), false);

  assert.equal(get3dsProductPath('/games/title.3ds'), '/games/title.z3ds');
  assert.equal(get3dsProductPath('/games/title.iso'), null);

  assert.equal(
    getDolphinProductPath({
      path: '/games/game.iso',
      has_rvz: true,
      dolphin_ready: true,
      dolphin_path: '/out/game.rvz',
    }),
    '/out/game.rvz',
  );

  assert.equal(
    getDolphinProductPath({
      path: '/games/game.iso',
      has_rvz: true,
      dolphin_ready: true,
      dolphin_path: '',
    }),
    '/games/game.rvz',
  );

  assert.equal(
    buildCompressionValue(['lzma', 'zlib'], [
      { value: 'none' },
      { value: 'zlib' },
      { value: 'lzma' },
    ]),
    'zlib,lzma',
  );

  assert.equal(
    buildCompressionValue(['none', 'zlib'], [
      { value: 'none' },
      { value: 'zlib' },
    ]),
    'none',
  );

  const original = new Map([['a', 1]]);
  const cloned = cloneSelectionMap(original);
  assert.deepEqual([...cloned.entries()], [['a', 1]]);
  assert.notEqual(cloned, original);

  const empty = cloneSelectionMap({});
  assert.equal(empty instanceof Map, true);
  assert.equal(empty.size, 0);

  withMockLocalStorage({
    getItem: () => '{invalid-json',
  }, () => {
    assert.deepEqual(loadStoredConversionPresets(), []);
  });

  withMockLocalStorage({
    getItem: (key) => {
      assert.equal(key, CONVERSION_PRESETS_STORAGE_KEY);
      return JSON.stringify({
        id: 'not-an-array',
      });
    },
  }, () => {
    assert.deepEqual(loadStoredConversionPresets(), []);
  });

  withMockLocalStorage({
    getItem: () => JSON.stringify([
      null,
      { id: '', name: 'Bad', isoHandling: 'chdman', conversionMode: 'createcd' },
      { id: 'bad-iso', name: 'Bad', isoHandling: 'broken', conversionMode: 'createcd' },
      {
        id: 'good',
        name: '  My Preset  ',
        isoHandling: 'dolphin',
        conversionMode: 'dolphin_rvz',
        compressionSelection: ['', 'zstd'],
        dolphinCompressionLevel: '12',
        outputDir: 1234,
        deleteOnVerify: 1,
      },
    ]),
  }, () => {
    const presets = loadStoredConversionPresets();
    assert.equal(presets.length, 1);
    assert.equal(presets[0].id, 'good');
    assert.equal(presets[0].name, 'My Preset');
    assert.equal(presets[0].isoHandling, 'dolphin');
    assert.equal(presets[0].outputDir, '');
    assert.equal(presets[0].deleteOnVerify, true);
    assert.deepEqual(presets[0].compressionSelection, ['zstd']);
  });

  withMockLocalStorage({
    getItem: () => JSON.stringify([
      {
        id: 'fallback',
        name: 'Fallback',
        isoHandling: 'chdman',
        conversionMode: 'createcd',
      },
    ]),
  }, () => {
    const presets = loadStoredConversionPresets();
    assert.equal(presets.length, 1);
    assert.deepEqual(presets[0].compressionSelection, ['zlib']);
    assert.equal(presets[0].dolphinCompressionLevel, '5');
  });

  withMockLocalStorage({
    getItem: () => {
      throw new Error('storage disabled');
    },
  }, () => {
    assert.deepEqual(loadStoredConversionPresets(), []);
  });

  const presetId1 = makeConversionPresetId();
  const presetId2 = makeConversionPresetId();
  assert.match(presetId1, /^preset-/);
  assert.match(presetId2, /^preset-/);
  assert.notEqual(presetId1, presetId2);
}

run();
console.log('Utility tests passed.');
