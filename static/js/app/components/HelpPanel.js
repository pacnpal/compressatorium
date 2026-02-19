import { html } from '../runtime/preactRuntime.js';
import { getPrimaryToolLabel } from '../utils/uiHelpers.js';

export function HelpPanel({ onClose, isoHandling }) {
    const toolLabel = getPrimaryToolLabel(isoHandling);
    return html`
        <div class="help-panel">
            <div class="help-header">
                <h3>Quick Start Guide</h3>
                <button class="btn btn-sm btn-secondary" onClick=${onClose}>×</button>
            </div>
            <div class="help-content">
                <h4>How to use Compressatorium</h4>
                <ol>
                    <li><strong>Select Primary Tool</strong> - Choose CHDMAN, Dolphin, 3DS, or igir at the top</li>
                    <li><strong>Select a Volume</strong> - Choose a mounted directory from the left panel</li>
                    <li><strong>Browse Files</strong> - Navigate through folders to find your disc images</li>
                    <li><strong>Select Files</strong> - Click checkboxes next to files you want to convert</li>
                    <li><strong>Choose Mode</strong>:
                        <ul>
                            <li><em>CHDMAN</em> - Create/Extract/Copy CHD files (CD/DVD/LaserDisc)</li>
                            <li><em>Dolphin</em> - Convert GameCube/Wii images (RVZ/WIA/GCZ/ISO)</li>
                            <li><em>3DS</em> - Compress Nintendo 3DS ROMs (.cci/.cia/.3ds → .zcci/.zcia/.z3ds)</li>
                            <li><em>igir</em> - ROM collection manager (copy/move/organize/verify/clean using DAT files)</li>
                        </ul>
                    </li>
                    <li><strong>Queue</strong> - Click the action button to add jobs to the queue</li>
                </ol>
                <h4>File Types</h4>
                <ul>
                    <li>💽 <strong>.gdi, .cue, .bin</strong> - Can be converted to CHD (CHDMAN)</li>
                    <li>🧭 <strong>.iso</strong> - Handled by ${toolLabel} for info/verify operations</li>
                    <li>💿 <strong>.chd</strong> - MAME CHD format (click to view information)</li>
                    <li>🎮 <strong>.rvz, .wia, .gcz, .wbfs</strong> - GameCube/Wii images (Dolphin)</li>
                    <li>🎮 <strong>.cci, .cia, .3ds</strong> - Nintendo 3DS ROMs:
                        <ul style="margin-top: 5px; font-size: 0.9em;">
                            <li><em>.cci</em> - CCI (Cart Image) format, cartridge dumps</li>
                            <li><em>.cia</em> - CIA (Installable Archive) format, updates/DLC</li>
                            <li><em>.3ds</em> - Alternative cart dump format (same as .cci)</li>
                            <li><em>Outputs:</em> .zcci, .zcia, .z3ds (compressed with ZStandard)</li>
                        </ul>
                    </li>
                    <li>📦 <strong>.zip, .7z, .rar</strong> - Archives (click to browse contents)</li>
                </ul>
                <h4>Compression Tips</h4>
                <ul>
                    <li><strong>CHDMAN:</strong> zlib is most compatible; lzma yields smaller files but slower encoding</li>
                    <li><strong>Dolphin:</strong> RVZ is recommended for best compression with fast decompression</li>
                    <li><strong>3DS:</strong> Uses seekable ZStandard compression (~50% size reduction)
                        <ul style="margin-top: 5px; font-size: 0.9em;">
                            <li>Natively supported by Azahar emulator (v2123+)</li>
                            <li>.3ds and .cci are the same format with different extensions</li>
                            <li>ROMs must be decrypted before compression</li>
                        </ul>
                    </li>
                    <li><strong>Delete-on-verify:</strong> Automatically removes source files after successful conversion</li>
                </ul>
                <p class="compression-note">
                    Omitting <code>-c</code> would use chdman defaults; this app always sends an explicit choice to avoid surprises.
                </p>
                <h4>Dolphin Formats</h4>
                <ul>
                    <li><strong>RVZ</strong> is the recommended format for Dolphin emulator.</li>
                    <li><strong>zstd</strong> compression gives the best speed/size balance for RVZ.</li>
                    <li><strong>Compression levels</strong> are required for RVZ/WIA (for example: <code>zstd:5</code>).</li>
                    <li><strong>WIA</strong> is an older compressed format; prefer RVZ for new conversions.</li>
                    <li><strong>GCZ</strong> uses fixed deflate compression (no codec selection).</li>
                    <li><strong>ISO</strong> output extracts to uncompressed disc image.</li>
                </ul>
                <h4>igir ROM Management</h4>
                <ul>
                    <li><strong>Commands:</strong> copy, move, link, symlink (write) + extract, zip, test, clean, report</li>
                    <li><strong>DAT Files:</strong> Mount No-Intro / Redump DATs to <code>/dats</code> for matching and 1G1R filtering</li>
                    <li><strong>1G1R:</strong> Enable <em>--single</em> with language/region preferences to keep one ROM per game</li>
                    <li><strong>Filtering:</strong> Exclude demos, betas, BIOS, prototypes, homebrew, and more</li>
                    <li><strong>Clean:</strong> Remove unmatched files from the output directory (use dry-run first)</li>
                    <li><strong>Report:</strong> Generate a summary of matched/unmatched ROMs without modifying files</li>
                </ul>
            </div>
        </div>
    `;
}
