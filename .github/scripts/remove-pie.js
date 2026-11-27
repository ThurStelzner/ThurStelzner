#!/usr/bin/env node
'use strict';

// Remove o grafico de rosca de linguagens do github-profile-3d-contrib.
//
// A action (yoshi389111/github-profile-3d-contrib) desenha esse donut
// incondicionalmente quando o tipo e "normal" -- nao existe opcao de config
// para desliga-lo. Ele e o unico <g> de nivel superior com
// transform="translate(40, 520)" (x=40, y=850-260-70=520).
//
// Como o token fine-grained nao expoe commits privados por repositorio
// (commitContributionsByRepository), o donut fica sempre "CSS + other".
// Aqui a gente remove esse grupo do SVG para nao mostrar informacao errada.

const fs = require('fs');
const path = require('path');

const DIR = process.argv[2] || 'profile-3d-contrib';

const OPEN_RE = /<g\b[^>]*transform="translate\(40,\s*520\)"[^>]*>/;

function stripPieGroup(svg) {
    const m = OPEN_RE.exec(svg);
    if (!m) {
        return { svg, removed: false };
    }

    const start = m.index;
    let depth = 1;
    const tagRe = /<g\b|\/g>/g;
    tagRe.lastIndex = m.index + m[0].length;

    let end = -1;
    let t;
    while ((t = tagRe.exec(svg)) !== null) {
        if (t[0] === '/g>') {
            depth--;
            if (depth === 0) {
                end = t.index + 3;
                break;
            }
        } else {
            depth++;
        }
    }

    if (end < 0) {
        return { svg, removed: false };
    }

    return { svg: svg.slice(0, start) + svg.slice(end), removed: true };
}

function main() {
    if (!fs.existsSync(DIR)) {
        console.error(`Pasta "${DIR}" nao encontrada.`);
        process.exit(1);
    }

    const files = fs
        .readdirSync(DIR)
        .filter((f) => f.toLowerCase().endsWith('.svg'));

    if (files.length === 0) {
        console.error(`Nenhum SVG em "${DIR}".`);
        process.exit(1);
    }

    for (const file of files) {
        const filePath = path.join(DIR, file);
        const svg = fs.readFileSync(filePath, 'utf8');
        const { svg: out, removed } = stripPieGroup(svg);

        if (removed) {
            fs.writeFileSync(filePath, out);
            console.log(
                `Donut removido de ${file} (${svg.length} -> ${out.length} bytes)`,
            );
        } else {
            console.log(`Sem donut em ${file} (nada a fazer).`);
        }
    }
}

main();
