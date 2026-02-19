import { html } from '../runtime/preactRuntime.js';
import { DEFAULT_DOLPHIN_COMPRESSION_LEVEL } from '../constants/uiConstants.js';

export const isMacMetadataName = (name) => name === '.DS_Store' || name.startsWith('._') || name === '__MACOSX';

export function getModeTerm(isoHandling, kind) {
    if (kind === 'file') return 'file';

    if (isoHandling === 'dolphin') {
        if (kind === 'product') return 'Dolphin output';
        if (kind === 'verification') return 'disc image';
    } else if (isoHandling === 'z3ds') {
        if (kind === 'product') return 'compressed 3DS';
        if (kind === 'verification') return '3DS output';
    } else {
        if (kind === 'product') return 'CHD';
        if (kind === 'verification') return 'CHD';
    }

    return 'item';
}

export function normalizeDolphinLevel(value) {
    const raw = `${value ?? ''}`.trim();
    if (!raw) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    if (!/^\d+$/.test(raw)) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    return raw;
}

export function getPrimaryToolLabel(toolSelection) {
    if (toolSelection === 'chdman') return 'CHDMAN';
    if (toolSelection === 'dolphin') return 'Dolphin';
    if (toolSelection === 'z3ds') return '3DS';
    if (toolSelection === 'igir') return 'igir';
    return 'None selected';
}

export function getFilterOptions(tool) {
    const common = [
        { value: '', label: 'All Types' },
        { value: '.zip,.7z,.rar', label: 'Archives' },
        { value: '.chd', label: 'CHD Files' }
    ];

    if (tool === 'dolphin') {
        return [
            ...common,
            { value: '.iso,.rvz,.wia,.gcz,.wbfs', label: 'GameCube/Wii Images' },
            { value: '.iso', label: 'ISO Files' },
            { value: 'custom', label: 'Custom...' }
        ];
    }

    if (tool === 'z3ds') {
        return [
            ...common,
            { value: '.cci,.cia,.3ds', label: '3DS ROMs' },
            { value: 'custom', label: 'Custom...' }
        ];
    }

    // Default (CHDMAN)
    return [
        ...common,
        { value: '.cue,.bin,.gdi,.iso', label: 'Disc Images' },
        { value: '.iso', label: 'ISO Files' },
        { value: 'custom', label: 'Custom...' }
    ];
}

export function getPrimaryToolHint(toolSelection) {
    if (toolSelection === null) {
        return html`<span role="img" aria-label="Warning">⚠️</span> Please select your primary tool above to get started`;
    }
    if (toolSelection === 'chdman') {
        return html`Convert disc images to CHD format • Supports CD/DVD/LaserDisc`;
    }
    if (toolSelection === 'dolphin') {
        return html`Convert GameCube/Wii disc images • Supports RVZ, WIA, GCZ, ISO formats`;
    }
    if (toolSelection === 'z3ds') {
        return html`Compress Nintendo 3DS ROMs • Supports .cci/.cia/.3ds (cart & CIA) → .zcci/.zcia/.z3ds • ~50% size reduction • <strong>Decrypted ROMs only</strong>`;
    }
    if (toolSelection === 'igir') {
        return html`ROM collection manager • Copy, move, organize, verify, and clean ROMs using DAT files • 1G1R filtering`;
    }
    return html`Current: ${getPrimaryToolLabel(toolSelection)}`;
}
