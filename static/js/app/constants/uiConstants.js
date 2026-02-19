export const ISO_TOOL_STORAGE_KEY = 'primary_tool_preference';
export const CONVERSION_PRESETS_STORAGE_KEY = 'conversion_presets_v1';
export const MAX_CONVERSION_PRESETS = 25;
export const AUTO_QUEUE_CAP_PROMPT_THRESHOLD = 250;
export const AUTO_QUEUE_RECOMMENDED_CAP = 100;
export const DEFAULT_DOLPHIN_COMPRESSION_LEVEL = '5';
export const DEFAULT_PAGE_SIZE = '50';
export const DEFAULT_SEARCH_AUTO_RETURN_TO_FILE_LIST = true;
export const MAX_VISIBLE_CREATING_PLACEHOLDERS = 100;

export const PAGE_SIZE_OPTIONS = [
    { value: '25', label: '25' },
    { value: '50', label: '50' },
    { value: '100', label: '100' },
    { value: '250', label: '250' },
    { value: 'all', label: 'All' }
];

export const MODE_GROUPS = [
    {
        id: 'create',
        label: 'Create CHD',
        options: [
            { value: 'createcd', label: 'Create CD CHD (Dreamcast, PS1, Sega CD)' },
            { value: 'createdvd', label: 'Create DVD CHD (PSP, PS2)' },
            { value: 'createraw', label: 'Create Raw CHD' },
            { value: 'createhd', label: 'Create HD CHD' },
            { value: 'createld', label: 'Create LaserDisc CHD' }
        ]
    },
    {
        id: 'extract',
        label: 'Extract from CHD',
        options: [
            { value: 'extractcd', label: 'Extract CD (cue/bin)' },
            { value: 'extractdvd', label: 'Extract DVD (iso)' },
            { value: 'extractraw', label: 'Extract Raw' },
            { value: 'extracthd', label: 'Extract HD' },
            { value: 'extractld', label: 'Extract LaserDisc (avi)' }
        ]
    },
    {
        id: 'copy',
        label: 'Copy / Recompress',
        options: [
            { value: 'copy', label: 'Copy / Recompress CHD' }
        ]
    },
    {
        id: 'dolphin',
        label: 'Dolphin (GameCube/Wii)',
        options: [
            { value: 'dolphin_rvz', label: 'Convert to RVZ (recommended)' },
            { value: 'dolphin_wia', label: 'Convert to WIA' },
            { value: 'dolphin_gcz', label: 'Convert to GCZ' },
            { value: 'dolphin_iso', label: 'Convert to ISO (extract)' }
        ]
    },
    {
        id: 'z3ds',
        label: 'Nintendo 3DS',
        options: [
            { value: 'z3ds_compress', label: 'Compress to ZCCI/ZCIA/Z3DS' }
        ]
    }
];
