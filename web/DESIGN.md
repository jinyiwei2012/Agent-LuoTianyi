# AgentLuo Web Design System

## 0. Research Log

- Embedded refs: shortlisted Figma, Intercom, and Notion; picked Figma's tool-surface geometry plus the existing AgentLuo theme because this is a conversation workspace, not a marketing page.
- Lazyweb: skipped because the repository already defines the product palette and no external site was requested.
- Imagen drafts: skipped because the Live2D model is the required focal asset and is already shipped in `public/live2d`.
- Skipped lanes: no external design package was introduced; the project has no icon library and uses text-labelled controls for reliable Chinese accessibility.

## 1. Atmosphere & Identity

AgentLuo Web feels like a small rehearsal room for a digital singer: cool blue surfaces keep the work area calm, while cyan and soft rose accents mark live activity and emotion. The signature is a fixed Live2D stage beside a quiet, readable conversation stream; the stage is the visual instrument and the chat area is the control desk.

## 2. Color

The source of truth is `src/utils/theme.ts`. Components consume CSS variables populated from `THEMES.light` or `THEMES.dark` in `App.tsx`; no page owns a raw color.

| Role | CSS token | Source field | Usage |
|------|-----------|--------------|-------|
| Root canvas | `--color-root` | `root` | App background |
| Chat canvas | `--color-chat-list` | `chatList` | Message region |
| Surface | `--color-surface` | `surface` | Panels and controls |
| Alternate surface | `--color-surface-alt` | `surfaceAlt` | Quiet grouping |
| Elevated surface | `--color-elevated` | `elevated` | Menus and dialogs |
| Primary text | `--color-text` | `text` | Body and headings |
| Secondary text | `--color-text-muted` | `textMuted` | Metadata and hints |
| Soft text | `--color-text-soft` | `textSoft` | Supporting copy |
| Accent | `--color-accent` | `accent` | Primary actions and focus |
| Accent soft | `--color-accent-soft` | `accentSoft` | Selection and badges |
| Accent text | `--color-accent-text` | `accentText` | Links and accent labels |
| Border | `--color-border` | `border` | Form and panel boundaries |
| User bubble | `--color-user-bubble` | `userBubble` | User messages |
| Agent bubble | `--color-bot-bubble` | `botBubble` | Agent messages |
| Bubble text | `--color-bubble-text` | `bubbleText` | Agent message text |
| User bubble text | `--color-user-bubble-text` | `userBubbleText` | User message text |
| Danger surface | `--color-danger-surface` | `dangerSurface` | Error feedback |
| Danger text | `--color-danger-text` | `dangerText` | Error labels |
| Debug surface | `--color-debug-background` | `debugBackground` | Trace console |

### Rules

- Accent is reserved for action, selection, focus, and live status.
- Surface hierarchy uses tonal shifts first and the theme shadow token only for floating controls.
- Light mode keeps body text on theme surfaces; dark mode keeps the same semantic roles and contrast intent.

## 3. Typography

### Scale

| Level | Size | Weight | Line height | Usage |
|-------|------|--------|-------------|-------|
| Display | `clamp(2rem, 4vw, 3rem)` | 700 | 1.1 | Auth title |
| H1 | `2rem` | 700 | 1.2 | Page title |
| H2 | `1.5rem` | 700 | 1.3 | Section title |
| H3 | `1.125rem` | 650 | 1.4 | Card title |
| Body | `1rem` | 400 | 1.6 | Conversation and forms |
| Small | `0.875rem` | 400 | 1.5 | Supporting copy |
| Caption | `0.75rem` | 600 | 1.4 | Metadata and status |

### Font Stack

- Primary: `"Avenir Next", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`
- Mono: `"SFMono-Regular", Consolas, "Liberation Mono", monospace`

Body text never drops below `0.875rem`; long assistant messages use a readable measure and `overflow-wrap: anywhere`.

## 4. Spacing & Layout

All spacing derives from a 4px base unit.

| Token | Value | Usage |
|-------|-------|-------|
| `--space-1` | `4px` | Icon-to-label and tight metadata |
| `--space-2` | `8px` | Compact controls |
| `--space-3` | `12px` | Form field padding |
| `--space-4` | `16px` | Standard panel padding |
| `--space-5` | `20px` | Comfortable groups |
| `--space-6` | `24px` | Page gutters and card padding |
| `--space-8` | `32px` | Panel separation |
| `--space-10` | `40px` | Auth and page rhythm |
| `--space-12` | `48px` | Major separation |

The app shell is a `fixed-sidenav-shell` variant: the Live2D stage is fixed within the left grid region, while the chat or page body owns vertical scrolling. Chat uses one scroll owner, `.message-scroll`; the header and composer remain fixed grid rows. At widths below 900px the stage becomes a bounded top region and the chat body remains the only scrolling region.

## 5. Components

### Live2D Stage
- **Structure**: stage header, model host, expression/status strip.
- **Variants**: desktop side stage, narrow top stage, loading, failed, ready, touched feedback.
- **Spacing**: `--space-4` and `--space-6`.
- **States**: loading and error remain visible without blocking chat; expression label changes on command.
- **Accessibility**: host has a labelled region; visual touch feedback is supplementary.
- **Motion**: model runtime owns motion; CSS only fades status feedback.
- **Layout**: bounded aside; does not scroll.

### Message Bubble
- **Structure**: sender label, text or image body, metadata, optional audio action.
- **Variants**: user, agent, system, image, singing, waiting, failed, playing.
- **Spacing**: `--space-2` to `--space-4`.
- **States**: audio button communicates idle/playing; send status is text, not color alone.
- **Accessibility**: semantic list, button labels, image alt text, live thinking status.
- **Motion**: short opacity/transform entry only; disabled under reduced motion.
- **Layout**: stack item inside `.message-scroll`.

### Form Field and Action Button
- **Structure**: visible label, control, optional hint/error.
- **Variants**: text, password, textarea, checkbox, primary, secondary, danger.
- **Spacing**: `--space-2` and `--space-3`.
- **States**: default, hover, active, focus, disabled, loading, error.
- **Accessibility**: labels are not placeholders; focus ring uses a dashed accent outline.
- **Motion**: 100-150ms opacity/transform feedback.
- **Layout**: stack and cluster primitives.

### Workspace Menu
- **Structure**: labelled header action and a vertical command list.
- **Variants**: open, closed, unread badge, theme selector.
- **Spacing**: `--space-2` through `--space-4`.
- **States**: active page, hover, focus, disabled while saving.
- **Accessibility**: native buttons/select; menu is announced by `aria-expanded`.
- **Motion**: opacity and translate entry; reduced motion removes transform.
- **Layout**: anchored elevated panel; chat remains the scroll owner.

### Content Card
- **Structure**: heading row, content, optional footer actions.
- **Variants**: dynamic post, settings group, model configuration, debug panel.
- **Spacing**: `--space-4` to `--space-6`.
- **States**: loading, empty, error, saved.
- **Accessibility**: heading hierarchy and explicit status text.
- **Motion**: none by default; content state changes use opacity only.
- **Layout**: intrinsic grid item with `min-inline-size: 0`.

## 6. Motion & Interaction

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | `120ms` | `ease-out` | Button press and focus polish |
| Standard | `240ms` | `ease-in-out` | Menu and page surface changes |
| Emphasis | `420ms` | `cubic-bezier(0.16, 1, 0.3, 1)` | Auth surface entry and Live2D status |

Motion follows the beui.dev `button`, `tabs`, `drawer`, and `animated-badge` mechanisms in a CSS-only form because the project has no motion dependency. Only `transform` and `opacity` animate. `prefers-reduced-motion: reduce` disables non-essential transforms and transitions.

## 7. Depth & Surface

Strategy: mixed tonal shift plus restrained shadows.

- Panels use `--color-surface` / `--color-surface-alt` contrast and `--color-border` for form boundaries.
- Floating menus use `--color-elevated` and a shadow derived from `--color-shadow` with `color-mix`.
- Cards stay at one consistent `14px` radius; controls use `10px`, and circular geometry is reserved for status marks.

## 8. Accessibility Constraints & Accepted Debt

### Constraints

- WCAG 2.2 AA intent: body contrast at least 4.5:1, visible keyboard focus, native form semantics, and keyboard reachability for every command.
- `prefers-reduced-motion` is respected.
- Images have alt text; status and error changes use `aria-live`.
- Primary content reflows to one column at 375px without horizontal scrolling.

### Accepted Debt

| Item | Location | Why accepted | Owner / Exit |
|------|----------|--------------|--------------|
| Web localStorage stores LLM API keys in the existing migrated utility | `src/utils/llm_key_storage.ts` | Existing interface contract and no browser secure store | Replace with a browser credential store or server-mediated key vault in a security-focused task |
| `oh-my-live2d` has no public destroy method in 0.19.3 | `src/components/Live2DView.tsx` | Use the installed package and remove its mounted host on unmount | Revisit when package exposes lifecycle teardown |
