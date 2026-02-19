export const IGIR_COMMANDS = [
    { value: 'copy', label: 'Copy', group: 'write', description: 'Copy ROMs to output directory' },
    { value: 'move', label: 'Move', group: 'write', description: 'Move ROMs to output directory' },
    { value: 'link', label: 'Link', group: 'write', description: 'Create links in output directory' },
    { value: 'extract', label: 'Extract', group: 'archive', description: 'Extract archives (requires copy or move)' },
    { value: 'zip', label: 'Zip', group: 'archive', description: 'Compress output to ZIP (requires copy or move)' },
    { value: 'test', label: 'Test', group: 'verify', description: 'Test/verify written files' },
    { value: 'clean', label: 'Clean', group: 'organize', description: 'Remove unmatched files (requires DATs)' },
    { value: 'report', label: 'Report', group: 'organize', description: 'Generate CSV report' },
    { value: 'fixdat', label: 'Fixdat', group: 'organize', description: 'Generate fixdat for missing ROMs' },
    { value: 'dir2dat', label: 'Dir2Dat', group: 'organize', description: 'Create DAT from directory' },
    { value: 'playlist', label: 'Playlist', group: 'organize', description: 'Generate M3U playlists' },
];

export const IGIR_WRITE_COMMANDS = new Set(['copy', 'move', 'link']);
export const IGIR_COPY_MOVE_COMMANDS = new Set(['copy', 'move']);
export const IGIR_ARCHIVE_COMMANDS = new Set(['extract', 'zip']);

export const IGIR_WORKFLOW_GOALS = [
    { id: 'first_sort', label: 'First-Time Sort' },
    { id: 'merge_new', label: 'Merge New Into Golden Set' },
    { id: 'flash_cart_1g1r', label: 'Flash Cart 1G1R' },
    { id: 'report_missing', label: 'Report + Fixdat' },
    { id: 'clean_preview', label: 'Clean Dry Run' },
    { id: 'playlist_only', label: 'Playlist Generation' },
    { id: 'mame_rebuild', label: 'MAME Rebuild' }
];

export const IGIR_FILTER_PRESETS = {
    retail: {
        label: 'Only Retail',
        description: 'Exclude BIOS, demos, betas, samples, prototypes, unlicensed, and bad dumps',
        flags: { no_bios: true, no_demo: true, no_beta: true, no_sample: true, no_prototype: true, no_unlicensed: true, no_bad: true, only_retail: true }
    },
    flashCart1G1R: {
        label: 'Flash Cart (1G1R USA)',
        description: '1G1R curated set: USA retail only, no BIOS/demos/betas/protos/bad, prefer verified USA English ROMs',
        flags: { no_bios: true, no_demo: true, no_beta: true, no_sample: true, no_prototype: true, no_unlicensed: true, no_bad: true, only_retail: true },
        oneG1R: { single: true, preferLanguage: 'EN', preferRegion: 'USA,EUR,JPN', preferVerified: true, preferGood: true, preferRetail: true, preferRevision: 'newer' }
    },
    complete: {
        label: 'Complete Collection',
        description: 'All retail + licensed ROMs, exclude only bad dumps',
        flags: { no_bad: true }
    },
    homebrew: {
        label: 'Homebrew Only',
        description: 'Only homebrew ROMs',
        flags: { only_homebrew: true }
    },
    all: {
        label: 'All ROMs',
        description: 'Clear all filters',
        flags: {}
    }
};
