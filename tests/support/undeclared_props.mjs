// Lists props a Svelte component is given by a caller but never declares.
//
// Svelte 5 drops a prop the child's $props() does not name, silently. This
// parses every .svelte file under the directory in argv[2], finds each
// <Component ...> use of an imported .svelte file, and prints a JSON array of
// {file, component, prop, line} for every attribute the child cannot receive.
// A child that destructures `...rest` accepts anything and is skipped, and a
// spread at the call site is not checked (it is not statically known).
import { parse } from 'svelte/compiler';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(process.argv[2]);
const files = [];
(function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (p.endsWith('.svelte')) files.push(p);
  }
})(root);

function visit(node, fn) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    node.forEach((n) => visit(n, fn));
    return;
  }
  if (node.type) fn(node);
  for (const k of Object.keys(node)) if (k !== 'metadata') visit(node[k], fn);
}

const sources = new Map();
const trees = new Map();
const declared = new Map();
for (const f of files) {
  const src = fs.readFileSync(f, 'utf8');
  const tree = parse(src, { modern: true });
  sources.set(f, src);
  trees.set(f, tree);
  const info = { props: new Set(), rest: false };
  visit(tree.instance, (n) => {
    const isProps =
      n.type === 'VariableDeclarator' &&
      n.init?.type === 'CallExpression' &&
      n.init.callee.name === '$props';
    if (!isProps) return;
    if (n.id.type !== 'ObjectPattern') {
      info.rest = true;
      return;
    }
    for (const p of n.id.properties) {
      if (p.type === 'RestElement') info.rest = true;
      else info.props.add(p.key.name ?? p.key.value);
    }
  });
  declared.set(f, info);
}

const found = [];
for (const [f, tree] of trees) {
  const imports = new Map();
  visit(tree.instance, (n) => {
    if (n.type !== 'ImportDeclaration' || !n.source.value.endsWith('.svelte')) return;
    let target = n.source.value.replace(/^\$lib/, path.join(root, 'lib'));
    if (target.startsWith('.')) target = path.resolve(path.dirname(f), target);
    for (const s of n.specifiers) {
      if (s.type === 'ImportDefaultSpecifier') imports.set(s.local.name, target);
    }
  });
  visit(tree.fragment, (n) => {
    if (n.type !== 'Component') return;
    const target = imports.get(n.name);
    const info = target && declared.get(target);
    if (!info || info.rest) return;
    for (const a of n.attributes) {
      if (a.type === 'SpreadAttribute' || info.props.has(a.name)) continue;
      found.push({
        file: path.relative(root, f),
        component: path.relative(root, target),
        prop: a.name,
        line: sources.get(f).slice(0, a.start).split('\n').length,
      });
    }
  });
}
process.stdout.write(JSON.stringify(found, null, 1));
