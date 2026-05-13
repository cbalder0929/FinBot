Build the frontend shell for a web-based financial statement analyzer. This is a static frontend only — no real backend yet, but structured so a FastAPI backend can plug in later. Use vanilla HTML, CSS, and JavaScript (no React, no build tools). Tailwind via CDN is fine.

## Layout

Two-column responsive layout, full viewport height:
- Left column (40% width): file input controls
- Right column (60% width): animated robot
- On screens under 768px, stack vertically with robot on top

## Left column — top to bottom

1. **App header**: project name "FinScrape" (or similar) with a short tagline underneath
2. **Drop zone**: large dashed-border area, accepts drag-and-drop of multiple files. On dragover, the border should brighten and the background should shift subtly. Show a cloud-upload icon and "Drop files here" text when empty.
3. **Two picker buttons** side by side below the drop zone:
   - "Choose Files" — opens `<input type="file" multiple accept=".pdf,.csv,.xlsx,.xls">`
   - "Choose Folder" — opens `<input type="file" webkitdirectory multiple>`
4. **Staged files list**: appears once files are added. Each row shows file-type icon, filename (truncated if long), file size in KB/MB, and an X button to remove. Scrollable if more than ~5 files.
5. **CTA button** at the bottom: "Start Processing" — disabled state when no files staged, active purple gradient when ready. On click, triggers the mock processing flow.

## Right column — the robot

Build the robot as inline SVG so its parts can be animated with CSS. It should have:
- A head with two eyes (circles or rounded rectangles)
- A body with a small screen/display in the center
- Two arms
- An antenna on top with a small glowing tip

The robot has five visual states, each with distinct animation:
- **idle**: gentle floating bob, eyes blink occasionally, antenna pulses slowly
- **uploading**: arms move as if receiving files, screen shows an up-arrow
- **parsing**: head tilts side to side, eyes scan left-right, screen shows scanning lines
- **categorizing**: faster motion, screen shows a spinning/sorting icon, antenna glows brighter
- **done**: arms raise up in celebration, eyes become happy curves, antenna does a brief burst
- **error**: eyes become Xs, screen shows an exclamation, slight slump

Below the robot, show a status text line that updates with the current stage (e.g., "Waiting for files...", "Parsing transactions...", "Categorizing with AI...", "Done! 47 transactions processed", "Something went wrong").

Below the status text, a progress bar that fills as the mock job progresses.

## Mock processing flow

When "Start Processing" is clicked, run a `setTimeout`-driven sequence that walks the robot through each state with realistic timing:
- uploading: 1.5s
- parsing: 3s, progress bar fills 0→50%
- categorizing: 4s, progress bar fills 50→100%, status text updates with fake counts ("Categorizing transaction 23 of 47...")
- done: hold for 2s, then return to idle and clear the file list

Structure the state-update code so it's trivial to replace later with real polling of a `/api/jobs/{id}/status` endpoint. Use a single `setRobotState(state, statusText, progress)` function.

## Visual design

**Purple theme** — deep, modern, not cartoonish. Use these as a starting palette and refine for contrast:
- Background: very dark purple-black, like `#0F0A1E` or `#13102A`
- Surface/cards: slightly lighter purple, like `#1E1738` with subtle border
- Primary accent: vibrant purple, like `#A855F7` or `#9333EA`
- Secondary accent (for highlights and the robot glow): lighter violet, like `#C084FC`
- Success state: keep within the purple family — a magenta-leaning `#D946EF`
- Error state: a warm rose that complements purple, not pure red — try `#F43F5E`
- Text primary: near-white `#F5F3FF`
- Text secondary: muted lavender `#A78BFA`

Use subtle purple glows (box-shadow with low opacity) on interactive elements. The robot's antenna tip and eyes should emit a soft purple glow that intensifies during active states.

Buttons should use a subtle gradient (primary purple to a slightly deeper shade) with a glow on hover. The CTA button when active should pulse very gently to draw attention.

Typography: a clean modern sans-serif. Inter or system-ui is fine. Headings slightly heavier, body text comfortable line-height.

## Code organization

Keep it simple:
- `index.html` — markup
- `styles.css` — all styling, organized by section with clear comments
- `app.js` — file handling, state management, mock processing flow
- `robot.js` — robot state controller (the `setRobotState` function and animation triggers)

Use CSS custom properties for the color palette at the top of `styles.css` so the theme is easy to tweak in one place.

## Out of scope for this build

- No real file upload or backend calls — just stage files in memory
- No authentication
- No persistence — refreshing clears everything
- No real categorization logic

Build it as a polished, production-feeling shell. The robot is the centerpiece of the right column and should feel alive, not janky. Prioritize smooth animations and a cohesive purple aesthetic over feature breadth.