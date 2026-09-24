import { access, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const reportPath = process.argv[2];
if (!reportPath) throw new Error("usage: node scripts/normalize-lcov.mjs <lcov.info>");

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const report = await readFile(reportPath, "utf8");
let sourceRecords = 0;

const lines = await Promise.all(
    report.split("\n").map(async (line) => {
        if (!line.startsWith("SF:")) return line;

        sourceRecords += 1;
        const source = line.slice(3).replaceAll("\\", "/");
        const relativeSource = source.startsWith("frontend/") ? source.slice(9) : source;
        await access(resolve(frontendRoot, relativeSource));
        return `SF:frontend/${relativeSource}`;
    }),
);

if (sourceRecords === 0) throw new Error(`${reportPath} contains no source records`);
await writeFile(reportPath, lines.join("\n"));