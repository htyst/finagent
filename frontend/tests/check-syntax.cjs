// Syntax-only check. Full semantic checking remains `npm run build`.
const fs = require('node:fs');
const path = require('node:path');
const ts = require(process.env.TYPESCRIPT_PATH || 'typescript');
const root = path.join(__dirname, '..', 'src');
let failed = false;
let count = 0;
for (const file of fs.readdirSync(root).filter(name => /\.tsx?$/.test(name))) {
  const result = ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), {
    fileName: file, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX, isolatedModules: true },
  });
  for (const diagnostic of result.diagnostics || []) {
    if (diagnostic.category === ts.DiagnosticCategory.Error) {
      failed = true;
      console.error(file, ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'));
    }
  }
  count++;
}
console.log(JSON.stringify({ check: 'typescript-syntax-only', files: count, passed: !failed }));
process.exitCode = failed ? 1 : 0;
