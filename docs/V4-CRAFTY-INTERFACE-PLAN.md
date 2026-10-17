# 4.0 "Crafty Interface" — design note

> **STATUS: RELEASED as 4.0 (2026-10-17).** Approved with the user in conversation, after
> clickable mockups: <https://claude.ai/artifact/D5xJSv7dBt6kbVh2jKzbJf> (private to the user).
> Stages 0–4 are done and verified on Windows and Linux. Stage 0 picked **Silkscreen Bold**
> (headings), **Pixelify Sans** (UI) and **Pixel Operator Mono** (console); Departure Mono wasn't
> needed. Still open from Stage 5: in the menu layout, Worlds / Players / Backups / Update /
> Settings keep their current arrangement in the Crafty skin rather than one row per choice.
> After the first try on the laptop both looks were reworked to match their mockups exactly, and
> every tab and button is now drawn (`CraftyTab` / `CraftyButton` / `CraftyRadio`) for the
> bevel and the text shadow.

## What this version is

A second visual skin and a second layout for the same Bedrock Server Manager, styled after
Minecraft Bedrock's own settings and chat screens. The user picks one of three looks in
**Settings ▸ Interface**: **Classic** (today's 3.1 interface, unchanged and still fully
supported), **New coat of paint** (today's tab layout in a dark Minecraft skin, with pixel fonts,
bevelled buttons and I/O toggles), or **New layout** (the same skin with a menu on the left and
one choice per row on the right, Machines as its own page, a chat-style console, and Teleport as
its own menu item). Internally this is two independent switches, *look* (Classic / Crafty) ×
*layout* (tabs / left menu), drawn on top of the existing `active_access` / `ServerService`
layer. The switch takes effect immediately, without a restart, and no Server is touched by it.

**The only new feature is the look switch itself.** Everything else is graphics: new ways of
drawing what 3.1 already does. That covers the Teleport page, the chat-style console and its
display options (follow new lines, show times, filter, text size). They change how things are
shown, never what the Server does.

## What this version explicitly is not

- **Not new Server features.** No new operations, config keys or wire protocol. Everything the
  new looks show already exists in 3.1; a feature that needs a new host op belongs in its own
  release.
- **Not a rewrite of the engine or the tabs' logic.** `ServerService`, `RemoteAdminHost`, the
  update pipeline, backups and config stay as they are. The looks only draw, so the same click
  runs the same code path in all three looks.
- **Not a replacement for Classic.** Classic isn't frozen, deprecated or "legacy". It's the
  universal fallback: maintained, and covered by the regression suite like the new looks.
- **Not a translation.** English only. No language files, no Danish, no translation layer.
- **Not a theming system.** No user-made themes, colour pickers or a theme file format. There's
  Classic (light or dark, as today) and Crafty (dark only), and that's all.
- **Not Mojang's assets.** No Mojangles or Minecraft Ten font, no textures or sounds copied from
  the game, no Minecraft logo. Only openly licensed fonts (CC0 or OFL, with the licence text
  bundled), or a font of our own that isn't a copy.
- **Not a web app or a new GUI toolkit.** No HTML/Electron/Qt port. It stays one Python file on
  stdlib + tkinter, cross-platform. The HTML mockup is a picture, not the build.
- **Not dependent on installing anything system-wide.** Fonts load privately for the app's own
  process on Windows and Linux. Nothing goes into the system font folders, and there are no pip
  packages.

---

## Decisions already made (from the mockup session — don't relitigate)

- **Settings ▸ Interface ▸ Look and layout**: one row, three buttons **Classic** / **New coat of
  paint** / **New layout**. The active one is shown **green only, no checkmark**, the same way a
  selected tab or Minecraft's own selected option is shown. This "green = chosen, nothing else"
  rule holds for every choice in the Crafty looks.
- **Classic keeps its light/dark mode**; the two Crafty looks are **dark only**.
- **English only.**
- **New coat of paint** = today's top line + tab row + tab contents, in the Crafty skin: Minecraft
  colours, a pixel font, bevelled buttons, each section drawn as its own block, and **on/off
  settings as I/O toggles** instead of checkboxes.
- **New layout**:
  - **Top line** as today: Server strip, ✏ + −. **Machines** is a button there that opens a
    **separate Machines page** (every Machine and its Servers, Start/Stop, Add Machine,
    ‹ Back).
  - **Left menu** in three groups with small pixel icons: *Play* (Server, Worlds, Players,
    **Teleport**), *Set-up* (Configuration, Backups, Update), *General* (Settings).
  - **Right pane**: a section header (big blocky caps + grey line), then one choice per row:
    title, grey description underneath, control on the right (toggle, slider, dropdown, button).
  - **Configuration** grouped as Players / World / Network / Extras, with plain titles ("Allow
    cheats", not `allow-cheats`); numbers with a known range as **green sliders**; an
    "● Unsaved changes" bar with Undo / Save changes.
  - **Teleport is a menu item of its own**, not inside the console: big player buttons "Who
    should move?" / "To whom?" (offline players greyed), a big green **Teleport!**, and the
    answer line underneath.
  - **The console looks like Minecraft's chat**: lines bottom-left, a bottom bar with ⚙, the
    input field and a speech-bubble send button. ⚙ opens **Console options** (follow new lines,
    show times, only joins and my commands, Quick commands, text size). The Server page shows the
    last three lines and **Open console**.
- Answer lines (game mode, teleport) stay green ✓ / red ✗, as in 3.1.

## 1. The look switch

- New app-level setting **`ui_look`**: `"classic"` (default, so an upgrade looks exactly like
  3.1) / `"coat"` / `"layout"`, with its default in `DEFAULT_SETTINGS`.
- Choosing a look **rebuilds the window's content** with the other look and layout. Nothing lives
  only in widgets: Servers, consoles, connections and settings are all in `self.contexts`,
  `self.connections`, `self.config` and the access objects, so a rebuild loses nothing. What a
  rebuild must carry over:
  - the selected Server (or Fleet/Machine page) and the selected tab/menu item;
  - the console's text (it's replayed from the Server's ring buffer anyway);
  - **unsaved Configuration edits**: switching asks first, through the same
    `_confirm_leave_unsaved()` as switching Servers.
- Running Servers, open remote connections and Remote Administration are never touched by a
  switch. This is tested explicitly (§6).
- The three buttons exist in all three looks (in Classic as a plain button row), so you can
  always get back to Classic from wherever you are.

## 2. Architecture: two switches on one set of pages

Today each tab builds its widgets directly with `ttk` calls. 4.0 puts two thin layers between the
tabs and tkinter:

1. **A look object** (`ClassicLook` / `CraftyLook`) owns the palette, the fonts and the **widget
   factories** that every tab uses instead of calling `ttk` directly: `button(kind)`,
   `onoff(var)`, `choice(values)`, `slider(min, max)`, `entry()`, `block(title)`,
   `answer_line()`. `ClassicLook` returns exactly today's `ttk` widgets, so Classic stays
   pixel-identical. `CraftyLook` returns the Crafty widgets (below).
2. **A layout object** (`TabsLayout` / `MenuLayout`) owns the window's frame: the top line, then
   either the tab row (`ttk.Notebook`) or the left menu + right pane, plus the Machines page.
   It asks each **page** to draw itself into the parent it gets.
3. **Pages** (Server, Worlds, Players, Teleport, Configuration, Backups, Update, Settings) keep
   all of today's logic: they read and write through `self.active_access` and call the same
   handlers as now. What changes is that a page describes its content as **sections and rows**
   (`page.section(title, subtitle)`, `page.row(title, description, control)`), and the layout
   decides what a row looks like:
   - in the tab layout a row is a label + control, gridded the way today's tabs are;
   - in the menu layout a row is the Minecraft row (title, grey description, control on the
     right, separator line).

   That keeps one copy of every page: a new setting is added once and shows up correctly in all
   three looks.
4. **Teleport** gets its own page in the menu layout; in the tab layout it stays where it is
   today (under the console on the Server tab). It's the same page code; the layout decides
   where it goes. Same for the console: inline on the Server tab in the tab layout, a chat-style
   screen in the menu layout.

### Crafty widgets (pure tkinter)

- **Button**: a `tk.Label` or small `tk.Canvas` with a bevel (light top edge, dark bottom edge),
  states normal / hover / pressed / disabled / **chosen = green**. Keyboard: Tab focus with a
  visible focus ring, Space/Enter activates.
- **On/off toggle**: the Minecraft I/O switch, a canvas two cells wide; it wraps the same
  `tk.BooleanVar` today's `Checkbutton` uses, so every handler stays as is.
- **Slider**: a canvas track with a green fill and a bevelled thumb; drag, click and arrow keys;
  backed by a `tk.IntVar`.
- **Dropdown**: the light Minecraft field with a ▼, on top of `ttk.Combobox` (restyled) or a
  `tk.Menu`.
- **Section block / header / row**: plain frames with the palette colours.
- No images or textures from the game; the pixel icons are drawn from small hand-made 8×8 maps,
  as in the mockup.

## 3. Fonts

Candidates (all openly licensed, none of them a copy of Minecraft's own font):

| Font | Licence | Role |
|---|---|---|
| **Pixel Operator** (Jayvee Enaguas) | CC0 (public domain) | body text; Bold and Mono variants |
| **Pixelify Sans** | SIL OFL 1.1 | body text (used in the mockup) |
| **Silkscreen** | SIL OFL 1.1 | blocky section headers (used in the mockup) |
| **Departure Mono** (Helena Zhang) | SIL OFL 1.1 | console |

Rejected: **Monocraft** (OFL, but a deliberate remake of Minecraft's font, too close to
Mojang's). Last resort: a small pixel font of our own, if none of the above is crisp and readable
at the app's real sizes.

How a font is chosen and shipped:

- **Picked in Stage 0** by rendering real UI text in tkinter on Windows and Linux at the sizes
  the looks use. A pixel font must be drawn at a whole multiple of its pixel grid, so sizes are
  given in pixels (negative tk sizes), rounded to the grid after DPI scaling.
- **It must cover the letters that show up in the console and in names**: æ ø å Æ Ø Å and the
  rest of Latin-1. World names, chat and players are Danish even though the app is English.
  Missing glyphs mean that font is rejected for that role.
- **Shipped inside the single `.py` file** as base64 (pixel fonts are a few tens of KB), together
  with its licence text. At startup the font is written to the app's own config folder and loaded
  **for this process only**:
  - Windows: `AddFontResourceExW(path, FR_PRIVATE)` via ctypes;
  - Linux: `FcConfigAppFontAddFile` via ctypes on libfontconfig.

  Nothing is installed system-wide. If loading fails, the Crafty looks fall back to the system
  monospace font and log one warning; the app never refuses to start because of a font.
- The licences are listed in the About box and in the repo (`LICENSES/`).

## 4. The menu layout's pages

As in the mockup. Two notes:

- **Configuration titles and descriptions**: a built-in table maps known keys to a plain title,
  a one-line description, a group (Players / World / Network / Extras) and a control type
  (toggle, slider with range, dropdown with values, text). Unknown keys (a future Mojang setting)
  still show, under "Other", with the key itself as their title and a text field or toggle. So no
  setting is ever hidden, and the table can grow over time. The three hidden-setting checkboxes
  from 3.1 are rows in Extras.
- **Sliders only where the range is known** (max players, view distance, simulation distance,
  backups to keep); everything else stays a text field.

## 5. Build order

Each stage is verified on **Windows and Linux**. After every stage the **full regression suite
runs in Classic**, and from Stage 2 on in every finished look, before the next stage starts.

0. **Spikes** (throwaway, in the scratchpad): private font loading on Windows and Linux, from
   source and inside the PyInstaller/AppImage builds; crisp sizes at 100 %, 125 % and 150 %
   scaling; glyph coverage incl. æøå. One Crafty toggle, button and slider, timed with ~45
   Configuration rows. **Outcome: the chosen fonts, or the decision to draw our own.**
1. **Plumbing with no visible change**: `ui_look`, the look and layout objects, and every tab
   moved onto the widget factories and the section/row description, with `ClassicLook` +
   `TabsLayout` only. The Interface row exists, but only Classic can be chosen yet (the other two
   greyed out with "Coming in 4.0"). The full regression suite must stay green unchanged; that's
   the proof that Classic didn't move.
2. **New coat of paint**: `CraftyLook` (palette, fonts, Crafty widgets) on `TabsLayout`; live
   switching between Classic and New coat of paint.
3. **New layout**: `MenuLayout` (left menu, right pane, rows), the Machines page, the
   Configuration table with groups and sliders, the Teleport page, and the other pages as rows.
4. **The chat-style console** and Console options, and the Server page's console preview.
5. **Polish and proof**: every test in all three looks, new real-click tests per look, a visual
   review on both systems (the user checks screenshots on the laptop, since screen automation
   on Windows is unreliable), docs, and **release 4.0**.

Stages 1–4 aren't released one by one: `main` only gets them once the regression suite is green,
and the homelab only gets an intermediate build when the user wants to try it.

## 6. Testing

- **Every existing scratch test runs once per look** (a `--look` argument). Classic must pass
  unchanged; the Crafty looks must pass the same behaviour checks through their own widgets.
- **New tests:**
  - switching looks with Servers running and a remote Machine connected: no Server stops, no
    connection drops, the selected Server and console survive;
  - switching with unsaved Configuration edits asks first;
  - the font loads privately (nothing new in the system font folders) and falls back cleanly if
    it can't;
  - every Crafty control works with the keyboard alone;
  - the window at its default size (1200×700) and at a small size: nothing clipped, menu and
    rows still usable;
  - the Teleport page and the chat console drive the same answers as 3.1 (✓/✗ lines).
- Cross-platform as always: Windows and Linux (WSL), plus the homelab's Python 3.11.

## 7. Risks

- **tkinter's fidelity**: bevels, crisp pixel text and smooth sliders are all doable, but the
  result won't be pixel-perfect Minecraft. Stage 0 shows how close it gets before anything big is
  built.
- **Performance**: canvas-drawn controls are slower than native ones; Configuration has ~45 rows
  and the Fleet page can be long. Measured in Stage 0.
- **DPI scaling**: pixel fonts blur at fractional scales. Handled by rounding to the font's grid
  (§3), and checked at 125 % and 150 %.
- **The page refactor (Stage 1)** touches every tab. That's why it ships with no visible change
  and has to pass the whole existing suite before any Crafty drawing begins.
