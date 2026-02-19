#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';

const repoRoot = process.cwd();
const staticJsRoot = path.join(repoRoot, 'static', 'js');
const appJsRoot = path.join(staticJsRoot, 'app');

async function walkJsFiles(dir) {
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch (err) {
    if (err && err.code === 'ENOENT') return [];
    throw err;
  }

  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await walkJsFiles(fullPath)));
      continue;
    }
    if (entry.isFile() && fullPath.endsWith('.js')) {
      files.push(fullPath);
    }
  }
  return files;
}

function stripQueryAndHash(specifier) {
  return specifier.replace(/[?#].*$/, '');
}

function extractImportSpecifiers(source) {
  const matches = [];
  const importExportRegex = /(?:^|\s)(?:import|export)\s+(?:[^'"\n;]*?\s+from\s+)?['"]([^'"\n]+)['"]/gm;
  const dynamicImportRegex = /\bimport\s*\(\s*['"]([^'"\n]+)['"]\s*\)/gm;

  let m;
  while ((m = importExportRegex.exec(source)) !== null) {
    matches.push(m[1]);
  }
  while ((m = dynamicImportRegex.exec(source)) !== null) {
    matches.push(m[1]);
  }

  return matches;
}

function isRelativeSpecifier(specifier) {
  return specifier.startsWith('./') || specifier.startsWith('../');
}

function hasExplicitExtension(specifier) {
  const cleaned = stripQueryAndHash(specifier);
  return path.extname(cleaned) !== '';
}

function toRelative(filePath) {
  return path.relative(repoRoot, filePath).split(path.sep).join('/');
}

async function fileExists(filePath) {
  try {
    const stat = await fs.stat(filePath);
    return stat.isFile();
  } catch {
    return false;
  }
}

function buildCyclePath(stack, fromNode) {
  const idx = stack.indexOf(fromNode);
  if (idx === -1) return [...stack, fromNode];
  return [...stack.slice(idx), fromNode];
}

function moduleLayer(filePath) {
  const normalized = filePath.split(path.sep).join('/');
  if (normalized.includes('/static/js/app/components/')) return 'components';
  if (normalized.includes('/static/js/app/hooks/')) return 'hooks';
  if (normalized.includes('/static/js/app/features/')) return 'features';
  if (normalized.endsWith('/static/js/app/AppRoot.js')) return 'app_root';
  return 'other';
}

function isLayerViolation(fromLayer, resolvedPath) {
  const normalized = resolvedPath.split(path.sep).join('/');
  if (fromLayer === 'components') {
    return normalized.includes('/static/js/app/hooks/')
      || normalized.includes('/static/js/app/features/')
      || normalized.endsWith('/static/js/app/AppRoot.js');
  }
  if (fromLayer === 'hooks') {
    return normalized.includes('/static/js/app/components/')
      || normalized.includes('/static/js/app/features/')
      || normalized.endsWith('/static/js/app/AppRoot.js');
  }
  if (fromLayer === 'features') {
    return normalized.endsWith('/static/js/app/AppRoot.js');
  }
  return false;
}

async function main() {
  const errors = [];
  const jsFiles = await walkJsFiles(staticJsRoot);

  const appGraph = new Map();
  const appNodes = new Set();

  for (const filePath of jsFiles) {
    if (filePath.startsWith(appJsRoot + path.sep) || filePath === appJsRoot) {
      appNodes.add(filePath);
      appGraph.set(filePath, new Set());
    }
  }

  for (const filePath of jsFiles) {
    const source = await fs.readFile(filePath, 'utf8');
    const specifiers = extractImportSpecifiers(source);
    const fromLayer = moduleLayer(filePath);

    for (const specifier of specifiers) {
      if (!isRelativeSpecifier(specifier)) continue;

      const cleanedSpecifier = stripQueryAndHash(specifier);
      if (!hasExplicitExtension(specifier)) {
        errors.push(
          `Missing explicit extension in ${toRelative(filePath)}: "${specifier}"`,
        );
        continue;
      }

      const resolvedPath = path.resolve(path.dirname(filePath), cleanedSpecifier);
      const exists = await fileExists(resolvedPath);
      if (!exists) {
        errors.push(
          `Unresolved import in ${toRelative(filePath)}: "${specifier}" -> ${toRelative(resolvedPath)}`,
        );
        continue;
      }

      if (isLayerViolation(fromLayer, resolvedPath)) {
        errors.push(
          `Layer violation in ${toRelative(filePath)}: "${specifier}" -> ${toRelative(resolvedPath)}`,
        );
      }

      if (appNodes.has(filePath) && appNodes.has(resolvedPath)) {
        appGraph.get(filePath)?.add(resolvedPath);
      }
    }
  }

  const color = new Map(); // 0 = unvisited, 1 = visiting, 2 = done
  const stack = [];

  function dfs(node) {
    color.set(node, 1);
    stack.push(node);

    const neighbors = appGraph.get(node) || new Set();
    for (const neighbor of neighbors) {
      const state = color.get(neighbor) || 0;
      if (state === 0) {
        dfs(neighbor);
        continue;
      }
      if (state === 1) {
        const cycle = buildCyclePath(stack, neighbor)
          .map((p) => toRelative(p))
          .join(' -> ');
        errors.push(`Circular dependency detected: ${cycle}`);
      }
    }

    stack.pop();
    color.set(node, 2);
  }

  for (const node of appNodes) {
    if ((color.get(node) || 0) === 0) {
      dfs(node);
    }
  }

  if (errors.length > 0) {
    console.error('ESM import checks failed:\n');
    for (const err of errors) {
      console.error(`- ${err}`);
    }
    process.exit(1);
  }

  console.log(`ESM import checks passed (${jsFiles.length} JS files scanned).`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
