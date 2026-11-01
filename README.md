<div align="center">

<img src="https://raw.githubusercontent.com/ameyachopade/gitlytics/main/assets/logo.png" alt="Gitlytics Logo" width="150" />

# Gitlytics v0.7.0
### GitHub Traffic Analytics, Dynamic Widgets & Automation

[![License](https://api.gitlytics.dev/api/badge/tech.svg?slug=license&style=icon_label_value&label=License&label_color=%23555555&variant=plastic&value=Apache+2.0&value_color=%232F81F7)](LICENSE)
[![Python](https://api.gitlytics.dev/api/badge/tech.svg?slug=python&style=icon_label_value&label=Python&label_color=%23555555&variant=plastic&value=3.9%2B&value_color=%233776AB)](https://www.python.org/)
[![PyPI](https://api.gitlytics.dev/api/badge/pypi/gitlytics.svg)](https://pypi.org/project/gitlytics/)
[![React](https://api.gitlytics.dev/api/badge/tech.svg?slug=react&style=icon_label_value&label=UI&label_color=%23555555&variant=plastic&value=React&value_color=%2361DAFB)](https://react.dev/)
[![FastAPI](https://api.gitlytics.dev/api/badge/tech.svg?slug=fastapi&style=icon_label_value&label=Backend&label_color=%23555555&variant=plastic&value=FastAPI&value_color=%23009688)](https://fastapi.tiangolo.com/)
[![Automation](https://api.gitlytics.dev/api/badge/tech.svg?slug=githubactions&style=icon_label_value&label=Data+Backup&label_color=%23555555&variant=plastic&value=Automation&value_color=%232088FF)](https://github.com/ameyachopade/gitlytics-github-traffic-automation)
[![Homepage](https://api.gitlytics.dev/api/badge/tech.svg?slug=homepage&style=icon_label_value&label=Homepage&label_color=%23555555&variant=plastic&value=gitlytics.dev&value_color=%233FB950)](https://gitlytics.dev)
[![Blog](https://api.gitlytics.dev/api/badge/tech.svg?slug=blog&style=icon_label_value&label=Blog&label_color=%23555555&variant=plastic&value=blog.gitlytics.dev&value_color=%233FB950)](https://blog.gitlytics.dev)
[![Live](https://api.gitlytics.dev/api/badge/tech.svg?slug=livedemo&style=icon_label_value&label=Live+Demo&label_color=%23555555&variant=plastic&value=dashboard.gitlytics.dev&value_color=%233FB950)](https://dashboard.gitlytics.dev)
[![Docs](https://api.gitlytics.dev/api/badge/tech.svg?slug=docs&style=icon_label_value&label=Docs&label_color=%23555555&variant=plastic&value=docs.gitlytics.dev&value_color=%233FB950)](https://docs.gitlytics.dev)

<br/>Please consider giving this project a ⭐ if you find it helpful! <br/>

**Beautiful GitHub traffic analytics for all your repositories — public and private.** <br/> Track views, clones, referrers, popular paths, stargazer trajectories, and health metrics indefinitely.

✨ **[Try the live dashboard at dashboard.gitlytics.dev](https://dashboard.gitlytics.dev)** ✨  
📝 **[Read engineering deep-dives at blog.gitlytics.dev](https://blog.gitlytics.dev)** ✨  
📚 **[Read the documentation at docs.gitlytics.dev](https://docs.gitlytics.dev)**

</div>

---

## 🎨 Interactive Profile Widgets, Cards & Badges

Elevate your GitHub profile README and developer portfolios with Gitlytics' native, dynamic widget rendering suite. Serve real-time stats cards, commit-level language breakdowns, repository health scorecards, concept tags, and tech badges.

### 📇 Profile & Repository Cards
Showcase your global activity or project metrics with gorgeous, theme-customizable cards:

<div align="center">
  <table border="0">
    <tr>
      <td align="center" width="50%">
        <b>👤 User Profile Details Card</b> (Tokyo Night + Coral Title)<br/>
        <img src="https://api.gitlytics.dev/api/cards/profile-details/demo.svg?theme=tokyonight&title=ff9e64&text=a9b1d6&accent_secondary=7aa2f7&v=2" alt="Profile Details" width="380" />
      </td>
      <td align="center" width="50%">
        <b>📈 Repository Metrics Card</b> (Nord + Dark Steel Slate)<br/>
        <img src="https://api.gitlytics.dev/api/cards/stats/demo.svg?theme=nord&title=88c0d0&text=d8dee9&bg=2e3440&border=4c566a&v=2" alt="Repository Stats" width="380" />
      </td>
    </tr>
    <tr>
      <td align="center" width="50%">
        <b>📊 Language Distribution Chart</b> (Radical + Pink Accents)<br/>
        <img src="https://api.gitlytics.dev/api/cards/repos-per-language/demo.svg?theme=radical&title=fe60a1&accent_secondary=a6e22e&v=2" alt="Language Distribution" width="380" />
      </td>
      <td align="center" width="50%">
        <b>🔥 Contribution Streak Tracker</b> (Solarized Dark + Olive green)<br/>
        <img src="https://api.gitlytics.dev/api/cards/contribution-streak/demo.svg?theme=solarized_dark&title=268bd2&text=859900&bg=002b36&hide_border=true&v=2" alt="Contribution Streak" width="380" />
      </td>
    </tr>
  </table>
</div>

### 🏷️ Multi-Source Tech Badges & Concept Capsules
Generate beautifully styled capsule badges for your tech stack. Support for **200+ technology logos** from official libraries, plus **custom text-only capsule badges** for concepts without brand icons (e.g. AI/ML methods, SRE, RAG):

<div align="center">
  
  **🛠️ Languages & Frameworks (Multi-color & Flat Brand Colors)**
  
  <img src="https://api.gitlytics.dev/api/logos/typescript.svg?style=logo_text&v=2" alt="TypeScript" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/python.svg?style=logo_text&v=2" alt="Python" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/react.svg?style=logo_text&v=2" alt="React" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/fastapi.svg?style=logo_text&v=2" alt="FastAPI" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/aws.svg?style=logo_text&v=2" alt="AWS" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/docker.svg?style=logo_text&v=2" alt="Docker" />
  
  <br/><br/>
  
  **🧠 AI/ML & Engineering Concepts (Text-Only Capsules)**
  
  <img src="https://api.gitlytics.dev/api/logos/agenticai.svg?v=2" alt="Agentic AI" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/rag.svg?v=2" alt="RAG" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/embeddings.svg?v=2" alt="Embeddings" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/vectorsearch.svg?v=2" alt="Vector Search" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/finetuning.svg?v=2" alt="Fine-Tuning" />
  
  <br/><br/>
  
  **🔗 Contact & Platform Connectors**
  
  <img src="https://api.gitlytics.dev/api/logos/github.svg?style=logo_text&v=2" alt="GitHub" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/linkedin.svg?style=logo_text&v=2" alt="LinkedIn" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/gmail.svg?style=logo_text&v=2" alt="Gmail" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/portfolio.svg?v=2" alt="Portfolio" /> &nbsp;
  <img src="https://api.gitlytics.dev/api/logos/x.svg?style=logo_text&v=2" alt="X" />

</div>

---

## 📌 Table of Contents

- [🔗 The Gitlytics Ecosystem](#-the-gitlytics-ecosystem)
- [🤖 Cloud AI Analysis & Automated Insights](#-cloud-ai-analysis--automated-insights)
- [📝 Blog & Engineering Space](#-blog--engineering-space)
- [🔑 Flexible Authentication Modes](#-flexible-authentication-modes)
- [🚨 Defeating GitHub's 14-Day Traffic Limit](#-defeating-githubs-14-day-traffic-limit)
- [🛠️ Installation](#installation)
- [⌨️ Core CLI Commands](#core-cli-commands)
  - [1️⃣ `gitlytics fetch`](#1-gitlytics-fetch-live-terminal-data)
  - [2️⃣ `gitlytics sync`](#2-gitlytics-sync-background-database-cron)
  - [3️⃣ `gitlytics dashboard`](#3-gitlytics-dashboard-react-web-ui)
  - [4️⃣ `gitlytics stars`](#4-gitlytics-stars-stargazer-history)
- [🐍 Native Python API](#native-python-api)
  - [1️⃣ `gitlytics.fetch_traffic()`](#1-gitlyticsfetch_traffic)
  - [2️⃣ `gitlytics.sync()`](#2-gitlyticssync)
  - [3️⃣ `gitlytics.serve_dashboard()`](#3-gitlyticsserve_dashboard)
  - [4️⃣ `gitlytics.fetch_star_history()`](#4-gitlyticsfetch_star_history)
- [📊 CSV Database Schema](#csv-database-schema)
- [🌟 Show Your Support](#show-your-support)
- [📄 License](#license)

---

## 🔗 The Gitlytics Ecosystem

The full Gitlytics platform consists of modular open-source components:

- 📊 **[Gitlytics Live Dashboard](https://dashboard.gitlytics.dev)**: Production web interface to visualize repository traffic, trends, and historical charts.
- 📝 **[Gitlytics Blog](https://blog.gitlytics.dev)**: Engineering deep-dives, comparison guides, and open-source analytics tutorials.
- 📚 **[Gitlytics Metadata Pages & Docs](https://docs.gitlytics.dev)**: Technical reference, badge explorer, and CLI documentation.
- ⚙️ **[Gitlytics Automation](https://github.com/ameyachopade/gitlytics-github-traffic-automation)**: GitHub Action companion tool that fetches and backs up traffic data automatically.

---

## 🤖 Cloud AI Analysis & Automated Insights

Gitlytics Cloud includes an automated AI intelligence engine powered by LLMs (Groq API) and cloud database synchronization (Supabase):

- **Automated AI Repository Health Checks**: Analyzes repository traffic trends, CI stability, PR cycle times, and contributor activity to produce automated health scorecards.
- **Anomaly & Spike Detection**: Identifies traffic surges, viral referrer shifts, and clone spikes, surfacing actionable insights.
- **Cloud Persistence & Multi-Repo Aggregation**: Synchronizes historical snapshots to cloud storage, enabling seamless multi-repository comparison and organization-wide analytics.
- **Weekly Email Digest Reports**: Delivers automated weekly summaries (via Resend API) summarizing top referral channels, view growth, and stargazer milestones directly to your inbox.

---

## 📝 Blog & Engineering Space

Explore our official engineering publication at **[blog.gitlytics.dev](https://blog.gitlytics.dev)**:

- **Engineering Deep-Dives**: Learn how Gitlytics solves GitHub's 14-day traffic retention cap, renders high-performance SVG badges, and handles multi-repository data pipelines.
- **Comparison Guides**: Detailed breakdowns comparing Gitlytics with native GitHub Insights, Shields.io, and third-party analytics platforms.
- **AI Agent Context**: The blog provides a dedicated structured text endpoint ([`https://blog.gitlytics.dev/llms-full.txt`](https://blog.gitlytics.dev/llms-full.txt)) formatted specifically for consumption by AI agents and LLM tools.

---

## 🔑 Flexible Authentication Modes

Gitlytics v0.7.0 supports 3 authentication modes:

1. **Token Authentication (PAT)**: Pass your GitHub Personal Access Token (Classic or Fine-Grained) with `repo` scope to fetch private & public repository metrics.
2. **Demo Mode (Instant Exploration)**: Explore the web dashboard and CLI immediately using curated sample data without entering any credentials.
3. **Headless TV & Kiosk Mode**: Pre-authenticate sessions for office monitors or team dashboards:
   ```bash
   gitlytics dashboard --token "ghp_xxxx" --data-dir "./data"
   ```

---

## 🚨 Defeating GitHub's 14-Day Traffic Limit

> **⚠️ Did you know?** GitHub normally **only saves your repository traffic data for 14 days**. After two weeks, your valuable views and clones data is permanently deleted.

We built a companion automation tool that runs silently every 13 days using GitHub Actions to snapshot and preserve your traffic data.

👉 **[Set up GitHub Traffic Automation here](https://github.com/ameyachopade/gitlytics-github-traffic-automation)** (Takes under 2 minutes to set up!)

Once your traffic snapshots are saved, Gitlytics automatically merges overlaps to build continuous historical analytics over months or years.

---

## 🛠️ Installation

Install via PyPI:

```bash
# Basic CLI and Python Core Module
pip install gitlytics

# Full installation (includes React Dashboard web engine)
pip install "gitlytics[dashboard]"
```

---

## ⌨️ Core CLI Commands

Gitlytics provides 4 primary command-line utilities:

### 1️⃣ `gitlytics fetch` (Live Terminal Data)
Fetch your live 14-day traffic and print ASCII tables directly in your console.
```bash
gitlytics fetch --token ghp_your_token_here --print-table

# Fetch specific metrics only (e.g., views and clones)
gitlytics fetch --token ghp_your_token_here --print-table --metrics views clones
```

### 2️⃣ `gitlytics sync` (Background Database Cron)
Append traffic snapshots to local CSV databases. Can run as a background cron job:
```bash
# Sync once
gitlytics sync --token ghp_your_token --data-dir ./data

# Run as a background cron job (runs daily at 11:00 PM)
gitlytics sync --token ghp_your_token --data-dir ./data --schedule-cron "0 23 * * *"
```

### 3️⃣ `gitlytics dashboard` (React Web UI)
Launch the React + FastAPI web dashboard.
```bash
gitlytics dashboard
```

### 4️⃣ `gitlytics stars` (Stargazer History)
Fetch the historical cumulative stargazer growth trajectory for a repository.
```bash
gitlytics stars owner/repo --token ghp_your_token
```

---

## 🐍 Native Python API

Import Gitlytics into Python applications to build custom analytics pipelines or host the web engine programmatically.

### 1️⃣ `gitlytics.fetch_traffic()`
```python
import gitlytics

# Fetch traffic for all accessible repositories
df = gitlytics.fetch_traffic(
    token="ghp_your_token",
    return_format="dataframe"  # Options: "dataframe", "timeseries", or "summary"
)
```

### 2️⃣ `gitlytics.sync()`
```python
import gitlytics

# Sync snapshots and export consolidated JSON for the dashboard
gitlytics.sync(
    token="ghp_your_token",
    data_dir="./data",
    export_json="./data/export.json"
)
```

### 3️⃣ `gitlytics.serve_dashboard()`
```python
import gitlytics

# Serve the dashboard programmatically on a custom port
gitlytics.serve_dashboard(
    host="0.0.0.0",
    port=8080,
    token="ghp_your_token",
    data_dir="./data"
)
```

### 4️⃣ `gitlytics.fetch_star_history()`
```python
import gitlytics

points = gitlytics.fetch_star_history(
    owner="ameyachopade",
    repo="gitlytics",
    token="ghp_your_token"
)
```

---

## 📊 CSV Database Schema

Local CSV databases track up to 23 metrics per snapshot:

| Column | Type | Description |
|---|---|---|
| `date` | `str` | ISO date (`YYYY-MM-DD`) for this day's traffic snapshot. |
| `repository` | `str` | Full GitHub repository name (`owner/repo`). |
| `is_private` | `bool` | `True` if repository is private, `False` otherwise. |
| `views` | `int` | Total page views on this day. |
| `unique_visitors` | `int` | Unique visitors on this day. |
| `clones` | `int` | Total git clone operations on this day. |
| `unique_cloners` | `int` | Unique clone clients on this day. |
| `stars` | `int` | Current total star count snapshot. |
| `forks` | `int` | Current total fork count snapshot. |
| `language` | `str` | Primary programming language of the repository. |
| `topics` | `str` | JSON array containing repository tags/topics. |
| `watchers_count` | `int` | Total watchers of the repository. |
| `pushed_at` | `str` | Last push ISO timestamp. |
| `created_at` | `str` | Repository creation ISO timestamp. |
| `open_issues_count` | `int` | Total number of open issues. |
| `top_referrer` | `str` | Top external traffic referral source. |
| `top_referrer_views` | `int` | Views sent by the top referrer. |
| `top_referrer_uniques` | `int` | Uniques sent by the top referrer. |
| `_raw_referrers` | `str` | Raw JSON array of all referral sources. |
| `top_path` | `str` | Most visited repository file path. |
| `top_path_views` | `int` | Views for the top path. |
| `top_path_uniques` | `int` | Uniques for the top path. |
| `_raw_paths` | `str` | Raw JSON array of all popular paths. |

---

## 🌟 Show Your Support

If you find this project useful, please consider giving it a ⭐ on [GitHub](https://github.com/ameyachopade/gitlytics)!

## 📄 License
Licensed under the [Apache License 2.0](LICENSE).
