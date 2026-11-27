// Answers lib/whooper.py's connections step with TypeScript's own tools, as gopls answers it for Go: the declarations
// of JavaScript and TypeScript files, and the code that calls or uses a declaration, one level up. The step runs it
// with node, once for each checkout and typescript package it asks about:
//
//   node lib/ts-callers.mjs <request.json>
//
// The request is a JSON object:
//   typescript  The folder of the typescript package to load: the one the reviewed code's package installed.
//   root        The checkout, the head's or the merge base's, which every path below is relative to.
//   hidden      The files the review run changed or added in the checkout, by path, each with its text in the reviewed
//               commit, or null when the commit has no such file. TypeScript reads these as they are in the commit,
//               so neither the run's edits nor its probes reach the answers, and every other file from the checkout.
//   files       The files whose declarations to list, which needs no project.
//   projects    By the path of a tsconfig.json or jsconfig.json, the declarations whose callers to find in its project,
//               each as {id, path, name, calls}: with calls, for a function or method, the code that calls it, as
//               TypeScript's call hierarchy lists its incoming calls, and otherwise the code that uses it, as
//               TypeScript's references list them. A tsconfig.json that only references other projects, as a solution
//               does, stands for the one of them whose files include the declaration's.
//
// It prints each answer as soon as it has it, as a line of JSON, so the step keeps what came before its time limit:
//   {"file": <path>, "declarations": [{name, kind, line, start, end}, ...]}  for each of files, and for each file
//                                                                            that calls or uses a declaration, before
//                                                                            the declaration's sites
//   {"id": <id>, "sites": [[<path>, <line>], ...]}  where code in the checkout calls or uses the declaration
//   {"id": <id>, "error": <message>}                TypeScript could not answer for the declaration
//   {"project": <config>, "error": <message>}       TypeScript could not load the project
//   {"error": <message>}                            TypeScript could not run at all; it then exits with code 1
//
// A file's declarations are those at its top level: functions, classes, interfaces, type aliases, enums, and variables,
// each named as the file names it, and the methods, accessors, and properties holding a function of each class, named
// with their class, such as Cart.add. A function's or method's kind is func or method, a class's, interface's, alias's,
// or enum's type, a const's const, and a let's or var's var; a const holding an arrow function or function expression
// is a func, as TypeScript's call hierarchy treats it, and a let or var holding one is not. Each spans its lines from
// the comment lines directly above it to its end, and line is the line of its name.
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const print = (answer) => process.stdout.write(`${JSON.stringify(answer)}\n`);
// The first line of an error's message, which the step puts in a sentence.
const message = (error) => (error instanceof Error ? error.message : String(error)).split("\n")[0];

try {
  answer(JSON.parse(readFileSync(process.argv[2], "utf8")));
} catch (error) {
  print({ error: message(error) });
  process.exitCode = 1;
}

function answer({ typescript, root, hidden, files, projects }) {
  const { version } = JSON.parse(readFileSync(path.join(typescript, "package.json"), "utf8"));
  // The compiler and language service, which TypeScript 7, written in Go, does not ship.
  const compiler = path.join(typescript, "lib", "typescript.js");
  if (!existsSync(compiler)) {
    throw new Error(`TypeScript ${version} has no language service that node can load`);
  }
  const ts = createRequire(import.meta.url)(compiler);
  const committed = new Map(Object.entries(hidden).map(([file, text]) => [path.join(root, file), text]));
  // The checkout does not change while this runs, so each answer about it holds; asking again costs a system call,
  // which each of TypeScript's questions repeats for thousands of files.
  const remember = (ask) => {
    const answers = new Map();
    return (file) => {
      if (!answers.has(file)) answers.set(file, ask(file));
      return answers.get(file);
    };
  };
  const readFile = (file) => (committed.has(file) ? (committed.get(file) ?? undefined) : ts.sys.readFile(file));
  const fileExists = remember((file) => (committed.has(file) ? committed.get(file) !== null : ts.sys.fileExists(file)));
  const listed = new Set();

  function declarations(sourceFile) {
    const lines = sourceFile.text.split("\n");
    const lineOf = (position) => sourceFile.getLineAndCharacterOfPosition(position).line + 1;
    const found = [];
    const add = (first, last, name, kind, label) => {
      let start = lineOf(first.getStart(sourceFile, true));
      while (start > 1 && /^\s*(\/\/|\/\*|\*)/.test(lines[start - 2])) start -= 1;
      const position = label.getStart(sourceFile);
      found.push({ name, kind, line: lineOf(position), start, end: lineOf(last.getEnd()), position });
    };
    // A default export without a name goes by default, and the call hierarchy finds it from its keyword.
    const keyword = (node, kind) => node.name ?? node.getChildren(sourceFile).find((child) => child.kind === kind);
    const holdsFunction = ({ initializer }) =>
      !!initializer && (ts.isArrowFunction(initializer) || ts.isFunctionExpression(initializer));
    for (const statement of sourceFile.statements) {
      if (ts.isFunctionDeclaration(statement)) {
        const label = keyword(statement, ts.SyntaxKind.FunctionKeyword);
        add(statement, statement, statement.name?.text ?? "default", "func", label);
      } else if (ts.isClassDeclaration(statement)) {
        const name = statement.name?.text ?? "default";
        add(statement, statement, name, "type", keyword(statement, ts.SyntaxKind.ClassKeyword));
        for (const member of statement.members) {
          if (member.name && (ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) ||
              ts.isSetAccessorDeclaration(member) || (ts.isPropertyDeclaration(member) && holdsFunction(member)))) {
            add(member, member, `${name}.${member.name.getText(sourceFile)}`, "method", member.name);
          }
        }
      } else if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement) ||
          ts.isEnumDeclaration(statement)) {
        add(statement, statement, statement.name.text, "type", statement.name);
      } else if (ts.isVariableStatement(statement)) {
        const constant = (statement.declarationList.flags & ts.NodeFlags.Const) !== 0;
        const list = statement.declarationList.declarations;
        list.forEach((declaration, i) => {
          if (ts.isIdentifier(declaration.name)) {
            const kind = !constant ? "var" : holdsFunction(declaration) ? "func" : "const";
            const last = list.length === 1 ? statement : declaration;
            add(i === 0 ? statement : declaration, last, declaration.name.text, kind, declaration.name);
          }
        });
      }
    }
    return found;
  }

  function list(file, sourceFile) {
    if (!listed.has(file)) {
      listed.add(file);
      print({ file, declarations: declarations(sourceFile).map(({ position, ...declaration }) => declaration) });
    }
  }

  for (const file of files) {
    const name = path.join(root, file);
    list(file, ts.createSourceFile(name, readFile(name) ?? "", ts.ScriptTarget.Latest, true));
  }

  const parsed = new Map();
  function parse(config) {
    if (!parsed.has(config)) {
      parsed.set(config, ts.getParsedCommandLineOfConfigFile(config, {}, {
        useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames,
        getCurrentDirectory: () => root,
        readDirectory: ts.sys.readDirectory,
        fileExists,
        readFile,
        onUnRecoverableConfigFileDiagnostic: (diagnostic) => {
          throw new Error(ts.flattenDiagnosticMessageText(diagnostic.messageText, " "));
        },
      }));
    }
    return parsed.get(config);
  }

  function service(config, names) {
    const settings = parse(config);
    const roots = [...new Set([...settings.fileNames, ...names])];
    return ts.createLanguageService({
      getCompilationSettings: () => settings.options,
      getScriptFileNames: () => roots,
      getScriptVersion: () => "1",
      getScriptSnapshot: (file) => {
        const text = readFile(file);
        return text === undefined ? undefined : ts.ScriptSnapshot.fromString(text);
      },
      getCurrentDirectory: () => path.dirname(config),
      getDefaultLibFileName: (options) => ts.getDefaultLibFilePath(options),
      fileExists,
      readFile,
      readDirectory: ts.sys.readDirectory,
      directoryExists: remember(ts.sys.directoryExists),
      getDirectories: ts.sys.getDirectories,
      realpath: remember(ts.sys.realpath),
      useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
    }, ts.createDocumentRegistry());
  }

  for (const [config, queries] of Object.entries(projects)) {
    try {
      // A solution, a tsconfig.json that only references other projects, holds none of the declarations' files, so each
      // declaration goes to the project it references whose files include the declaration's.
      const top = path.join(root, config);
      const groups = new Map();
      for (const query of queries) {
        const name = path.join(root, query.path);
        const owner = parse(top).fileNames.includes(name) ? top : (parse(top).projectReferences ?? [])
          .map((reference) => ts.resolveProjectReferencePath(reference))
          .find((referenced) => parse(referenced).fileNames.includes(name)) ?? top;
        groups.set(owner, [...(groups.get(owner) ?? []), query]);
      }
      for (const [owner, group] of groups) {
        const languageService = service(owner, group.map((query) => path.join(root, query.path)));
        for (const query of group) {
          try {
            print({ id: query.id, sites: sites(languageService, query) });
          } catch (error) {
            print({ id: query.id, error: message(error) });
          }
        }
      }
    } catch (error) {
      print({ project: config, error: message(error) });
    }
  }

  function sites(languageService, { path: file, name, calls }) {
    const program = languageService.getProgram();
    const sourceFile = program.getSourceFile(path.join(root, file));
    const declaration = sourceFile && declarations(sourceFile).find((found) => found.name === name);
    if (!declaration) {
      throw new Error(`its project leaves out ${file}, or TypeScript finds no ${name} there`);
    }
    const spans = calls
      ? languageService.provideCallHierarchyIncomingCalls(sourceFile.fileName, declaration.position)
        .flatMap((call) => call.fromSpans.map((span) => [call.from.file, span.start]))
      : (languageService.findReferences(sourceFile.fileName, declaration.position) ?? [])
        .flatMap((symbol) => symbol.references.map((reference) => [reference.fileName, reference.textSpan.start]));
    const found = [];
    for (const [fileName, start] of spans) {
      const relative = path.relative(root, fileName).split(path.sep).join("/");
      const site = program.getSourceFile(fileName);
      if (site && !relative.startsWith("../") && !path.isAbsolute(relative)) {
        list(relative, site);
        found.push([relative, site.getLineAndCharacterOfPosition(start).line + 1]);
      }
    }
    return found;
  }
}
