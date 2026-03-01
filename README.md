![Build Status](https://github.com/eightmouse/Innkeper/actions/workflows/build.yml/badge.svg)
[![GitHub release](https://img.shields.io/github/v/release/eightmouse/Innkeper)](https://github.com/eightmouse/Innkeper/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Support me on Ko-fi](https://img.shields.io/badge/Support%20Me-Ko--fi-F16061?logo=ko-fi&logoColor=white)](https://ko-fi.com/eightmouse)

# Innkeeper

## Disclaimer
This app does NOT replace Armory or WoWHead in any way, shape or form. <br />
I strongly suggest using those since they have everything, quite thankful for them especially WoWHead, amazing resource!

## Description 
Innkeper it's a minimal World of Warcraft app to keep track of character informations. <br />
Built using Python backend for data processing and an Electron frontend for the user interface. <br />
Utilizing the official Blizzard API and supplemental data from WoWHead, providing a low-latency alternative to traditional web-based armory tools. <br />

It's a personal project that I started a while ago to push my skills further while taking breaks between WoW sessions. <br />
Started building it few weeks prior to pushing it to GitHub, mostly due to the fact that I was not planning on actually developing something 'complete' but here we are. <br />

Innkeeper was born out of a simple need: I was tired of alt-tabbing to WoWHead just to check a build or a timer. I wanted a 'digestible' desktop companion that felt like part of the game UI.<br />
Started WoW not long ago and I found myself closing and opening WoWhead for builds over and over, I wanted something on my desktop that I could access quickly, because I'm lazy.<br />

## Supported Regions
Innkeeper utilizes the Blizzard Battle.net API to fetch character data from the following regions:
* **North America (NA)**
* **Europe (EU)**
* **Korea (KR)**
* **Taiwan (TW)**

## Key Features
Innkeeper brings your character data to your desktop without the overhead of a web browser.<br />
* **Progression Tracking:** Keep tabs on your Raid progress, Mythic+ ratings, and World Quest completion at a glance.
* **Gear:** View your current character equipment and profession status without logging in.
* **Talents:** Access talent builds, perfect for second-monitor reference while setting up for a run.
* **Light & Portable:** Zero installation required. One executable, zero bloat, runs everywhere.
* **Cross-Platform Portability:** Distributed as a standalone executable for Windows, Linux and MacOS to ensure a zero-footprint installation.

### Vault & Activities
* **Dynamic Prediction:** Aggregates Mythic+ and Raid data to forecast weekly rewards.
* **Manual World Ledger:** Track non-API activities like Prey tiers to complete your Vault forecast.
* **Stat Squish Optimized:** All values adjusted for the current expansion power scale.

### Profession Management
* **Concentration Forecast:** Predicts when your resources will cap (4-day full recharge).
* **Weekly Checklist:** Auto-resetting trackers for Treatises, Quests, and Mob Drops.
* **Moxie Tracking:** Monitor your profession-locked currency at a glance.

## Interface
<img width="400" height="640" alt="1" src="https://github.com/user-attachments/assets/d277fdbc-8ff2-42ea-a255-2bc8c32e20f0" />
<img width="400" height="640" alt="2" src="https://github.com/user-attachments/assets/72510ab0-5f76-4468-8215-77eddcea8520" />
<img width="400" height="640" alt="3" src="https://github.com/user-attachments/assets/79e0ac4c-6be8-4a31-a0d2-2065ef988dc4" />
<img width="400" height="640" alt="4" src="https://github.com/user-attachments/assets/b1a2aff9-fb1a-4eaa-807b-b1f40f8eab36" />
<img width="400" height="640" alt="5" src="https://github.com/user-attachments/assets/02d9cb21-2312-4f8f-baac-c22f060835d9" />
<img width="400" height="640" alt="6" src="https://github.com/user-attachments/assets/ab310f50-c1b4-4ac4-964a-99610fc90b8d" />
<img width="400" height="640" alt="7" src="https://github.com/user-attachments/assets/0c3d432e-6c2e-426e-bc50-044bc26cd779" />
<img width="400" height="640" alt="8" src="https://github.com/user-attachments/assets/cef9f2ed-c1f1-4a60-8e54-b12a6d9be1ab" />

## Getting Started
1. Go to the [Releases](https://github.com/eightmouse/Innkeper/releases) page.
2. Download the version for your OS (`.exe` for Windows, `.AppImage` for Linux, `.dmg` for MacOS).

## FaQ
***Q: Why does the app take a long time to load character data on the first launch?*** <br />
***A:*** *Innkeeper utilizes a backend hosted on a Render free instance. Due to the service's resource management, the server may enter a sleep state after a period of inactivity. The initial request of a session may require up to 60 seconds for the instance to "spin up." Subsequent interactions will be processed with standard latency.<br />*

***Q: Why Electron?*** <br />
***A:*** *I know that it's quite hated for it's memory usage, super valid and agreable critique, HOWEVER: <br />
It leverages modern web standards (HTML5/CSS3) for the user interface while maintaining a unified codebase for cross-platform distribution. I thought about other options but I'm not confident and skilled enough to use other frameworks. Tauri could've achieved same results with less memory usage but as I said, that would've been out of my skill reach for now!* <br />

***Q: Why Portable and no installer?*** <br />
***A:*** *Because installers can be bloat. I don't plan on having THAT many features to make an installer worth.* <br />
  *- For Windows: You get a standalone .exe. No registry changes, no "Program Files" clutter, no leftover junk.* <br />
  *- For Linux: You get an AppImage. It’s distro-agnostic and runs anywhere.* <br />
  *- For MacOS: Same reasoning applies!* <br />
  
***Q: Will you add X?*** <br />
***A:*** *As I mentioned, the goal for this app is not to replace the resources already available but more of a 'quick but less' alternative. So, suggestions are welcomed but I can't guarantee I will add specific things unless they're more of a QoL than anything.*

***Q: Did you use AI to help you develop the app?*** <br />
***A:*** *Yes. When used correctly it's an amazing tool that provides help. I see it no differently than using StackOverflow or Google, with the difference that at least I'm not get bullied for asking/expressing myself poorly. If this bothers you I apologize, this app might not be for you.*

***Q: I'm on Linux/MacOS can I still use this app?*** <br />
***A:*** *Like Ronnie Coleman once said, 'YEAH BUDDY!'.*


