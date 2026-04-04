Please note: This Project is Currently NOT Maintained.

# 📚 MyBibliotheca

# 2.0.1+
**⚠️ Warning**: MyBibliotheca is under heavy development. Always back up your data before upgrading. The developers do not guarantee data persistence or error-free operation. Please submit issues to the repository, and we will address them as soon as possible.

**MyBibliotheca** is a self-hosted personal library and reading-tracker—your open-source alternative to Goodreads, StoryGraph, and Fable! It lets you log, organize, and visualize your reading journey. Add books by ISBN, track reading progress, log daily reading, and generate monthly wrap-up images of your finished titles.

🆕 **Multi-User Features**: Multi-user authentication, user data isolation, admin management, and secure password handling.

[![Documentation](https://img.shields.io/badge/Documentation-MyBibliotheca-4a90e2?style=for-the-badge&logo=read-the-docs&logoColor=white)](https://mybibliotheca.org)

[![Discord](https://img.shields.io/badge/Discord-7289DA?logo=discord&logoColor=white&labelColor=7289DA&style=for-the-badge)](https://discord.gg/Hc8C5eRm7Q)

---

## 📸 Screenshots

### Library Homepage
Browse your personal book collection with beautiful cover displays, reading status indicators, and quick access to all your books.

![Library Homepage](https://i.imgur.com/cDN06Lo.png)

### Reading Log
Track your reading sessions with detailed logging including pages read, time spent, and personal notes for every book.

![Reading Log](https://i.imgur.com/1WqQQAW.png)

### Book Details
View comprehensive book information including genres, authors, reading status, publication dates, and manage your personal collection.

![Book Details](https://i.imgur.com/A4jI2nS.png)

---


---

## ✨ Features

- 📖 **Add Books**: Add books quickly by ISBN with automatic cover and metadata fetching. Now featuring bulk-import from Goodreads and other CSV files!
- ✅ **Track Progress**: Mark books as *Currently Reading*, *Plan to Read*, *Finished*, or *Library Only*.
- 📅 **Reading Logs**: Log daily reading activity and maintain streaks.
-  **Search**: Find and import books using the Google Books API.
- 📱 **Responsive UI**: Clean, mobile-friendly interface built with Bootstrap.
- 🔐 **Multi-User Support**: Secure authentication with user data isolation
- 👤 **Admin Management**: Administrative tools and user management
- � **Graph Database**: Powered by KuzuDB for advanced relationship modeling and queries

#### 🚀 Docker Quick Start: [View Documentation](https://mybibliotheca.org/)


## 🗂️ Project Structure

```
mybibliotheca/
├── app/
│   ├── __init__.py              # Application factory
│   ├── auth.py                  # Authentication routes
│   ├── domain/                  # Domain models and business logic
│   ├── infrastructure/          # KuzuDB connection and repositories
│   ├── routes/                  # Application routes
│   ├── services/                # Business logic services
│   ├── schema/                  # Database schema definitions
│   ├── templates/               # Jinja2 templates
│   ├── static/                  # Static assets (CSS, JS, images)
│   └── utils/                   # Utility functions
├── data/                        # Data directory (mounted volume)
│   ├── kuzu/                    # KuzuDB database files
│   ├── covers/                  # Book cover images
│   └── uploads/                 # User uploaded files
├── scripts/                     # Admin and utility scripts
├── docs/                        # Documentation
├── docker-compose.yml           # Docker Compose configuration
├── Dockerfile                   # Docker image definition
├── requirements.txt             # Python dependencies
├── run.py                       # Application entry point
└── README.md                    # This file
```

---

## 📄 License

Licensed under the [MIT License](LICENSE).

---

## ❤️ Contribute

**MyBibliotheca** is open source and contributions are welcome!

- 🐛 **Report Bugs**: Open an issue on GitHub
- 💡 **Feature Requests**: Submit ideas for new features
- 🔧 **Pull Requests**: Contribute code improvements
- 📖 **Documentation**: Help improve our docs
- 💬 **Community**: Join our [Discord](https://discord.gg/Hc8C5eRm7Q)

### Development Setup

```bash
# Fork and clone the repository
git clone https://github.com/pickles4evaaaa/mybibliotheca.git
cd mybibliotheca

# Create a branch for your changes
git checkout -b feature/my-new-feature

# Make your changes and test
docker compose -f docker-compose.dev.yml up -d

# Submit a pull request
```
---

### 📞 Getting Help

If you encounter issues:

1. **Check the logs**: `docker compose logs -f`
2. **Enable debug mode**: Add `MYBIBLIOTHECA_DEBUG=true` to `.env` and restart
3. **Search existing issues**: [GitHub Issues](https://github.com/pickles4evaaaa/mybibliotheca/issues)
4. **Ask for help**: [Discord Community](https://discord.gg/Hc8C5eRm7Q)
5. **Create an issue**: Include logs, environment details, and steps to reproduce

---
