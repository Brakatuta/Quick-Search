#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <io.h> 
#include <windows.h>
#include <wchar.h>
#include "sqlite3.h"

sqlite3 *db;
sqlite3_stmt *stmt;
long long global_count = 0;

void format_size(long long bytes, char *out) {
    if (bytes < 0) { strcpy(out, "..."); return; }
    const char *units[] = {"B", "KB", "MB", "GB", "TB"};
    int i = 0;
    double d_bytes = (double)bytes;
    while (d_bytes > 1024 && i < 4) { d_bytes /= 1024; i++; }
    sprintf(out, "%.1f%s", d_bytes, units[i]);
}

// recursive function to get folder size in C (blazing fast)
unsigned long long get_directory_size(const wchar_t *szDir) {
    WIN32_FIND_DATAW ffd;
    wchar_t szDirPlusEnd[MAX_PATH];
    unsigned long long size = 0;

    _snwprintf(szDirPlusEnd, MAX_PATH, L"%s\\*", szDir);
    HANDLE hFind = FindFirstFileW(szDirPlusEnd, &ffd);
    if (INVALID_HANDLE_VALUE == hFind) return 0;

    do {
        if (wcscmp(ffd.cFileName, L".") != 0 && wcscmp(ffd.cFileName, L"..") != 0) {
            if (ffd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                wchar_t szSubDir[MAX_PATH];
                _snwprintf(szSubDir, MAX_PATH, L"%s\\%s", szDir, ffd.cFileName);
                size += get_directory_size(szSubDir);
            } else {
                ULARGE_INTEGER fileSize;
                fileSize.LowPart = ffd.nFileSizeLow;
                fileSize.HighPart = ffd.nFileSizeHigh;
                size += fileSize.QuadPart;
            }
        }
    } while (FindNextFileW(hFind, &ffd) != 0);
    FindClose(hFind);
    return size;
}

// using Wide Characters to match Phase 2
void fast_count(const wchar_t *path) {
    WIN32_FIND_DATAW findData;
    wchar_t searchPath[MAX_PATH];
    // _snwprintf is the reliable Windows way to do swprintf with a size limit
    _snwprintf(searchPath, MAX_PATH, L"%s\\*", path);

    HANDLE hFind = FindFirstFileW(searchPath, &findData);
    if (hFind == INVALID_HANDLE_VALUE) return;

    do {
        if (wcscmp(findData.cFileName, L".") == 0 || wcscmp(findData.cFileName, L"..") == 0) continue;
        global_count++;
        if (findData.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            wchar_t nextPath[MAX_PATH];
            _snwprintf(nextPath, MAX_PATH, L"%s\\%s", path, findData.cFileName);
            fast_count(nextPath);
        }
        if (global_count % 5000 == 0) {
            printf("COUNT|%lld\n", global_count);
            fflush(stdout);
        }
    } while (FindNextFileW(hFind, &findData));
    FindClose(hFind);
}

void index_files(const wchar_t *path) {
    WIN32_FIND_DATAW findData; 
    wchar_t searchPath[MAX_PATH];
    _snwprintf(searchPath, MAX_PATH, L"%s\\*", path);

    HANDLE hFind = FindFirstFileW(searchPath, &findData);
    if (hFind == INVALID_HANDLE_VALUE) return;

    do {
        if (wcscmp(findData.cFileName, L".") == 0 || wcscmp(findData.cFileName, L"..") == 0) continue;

        wchar_t fullPath[MAX_PATH];
        _snwprintf(fullPath, MAX_PATH, L"%s\\%s", path, findData.cFileName);
        int is_dir = (findData.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY);
        
        char disp_sz[20];
        long long size = -1;

        if (!is_dir) {
            ULARGE_INTEGER fileSize;
            fileSize.LowPart = findData.nFileSizeLow;
            fileSize.HighPart = findData.nFileSizeHigh;
            size = fileSize.QuadPart;
            format_size(size, disp_sz);
        } else {
            strcpy(disp_sz, "...");
        }

        sqlite3_bind_text(stmt, 1, is_dir ? "Folder" : "File", -1, SQLITE_STATIC);
        sqlite3_bind_text16(stmt, 2, fullPath, -1, SQLITE_STATIC);
        sqlite3_bind_int64(stmt, 3, size);
        sqlite3_bind_text(stmt, 4, disp_sz, -1, SQLITE_STATIC);
        sqlite3_step(stmt);
        sqlite3_reset(stmt);

        global_count++;
        if (global_count % 1000 == 0) {
            printf("PROGRESS|%lld\n", global_count);
            fflush(stdout);
        }

        if (is_dir) index_files(fullPath);

    } while (FindNextFileW(hFind, &findData));
    FindClose(hFind);
}

void prune_database(sqlite3 *db) {
    sqlite3_stmt *p_stmt;
    sqlite3_stmt *del_stmt;
    
    printf("STATUS|Cleaning up stale entries...\n");
    fflush(stdout);

    sqlite3_prepare_v2(db, "SELECT path FROM files;", -1, &p_stmt, NULL);
    sqlite3_prepare_v2(db, "DELETE FROM files WHERE path = ?;", -1, &del_stmt, NULL);
    
    // FIX: Prune in batches of 1000 so the DB isn't locked for the entire duration
    sqlite3_exec(db, "BEGIN TRANSACTION;", NULL, NULL, NULL);

    int pruned = 0;
    int batch_counter = 0;
    while (sqlite3_step(p_stmt) == SQLITE_ROW) {
        const wchar_t *path = (const wchar_t*)sqlite3_column_text16(p_stmt, 0);
        if (_waccess(path, 0) != 0) {
            sqlite3_bind_text16(del_stmt, 1, path, -1, SQLITE_STATIC);
            sqlite3_step(del_stmt);
            sqlite3_reset(del_stmt);
            pruned++;
            batch_counter++;
        }

        if (batch_counter >= 1000) {
            sqlite3_exec(db, "COMMIT; BEGIN TRANSACTION;", NULL, NULL, NULL);
            batch_counter = 0;
        }
    }

    sqlite3_exec(db, "COMMIT;", NULL, NULL, NULL);
    sqlite3_finalize(p_stmt);
    sqlite3_finalize(del_stmt);
    printf("STATUS|Pruned %d missing items.\n", pruned);
    fflush(stdout);
}

int main(int argc, char *argv[]) {
    if (argc < 3) return 1;

    if (strcmp(argv[1], "size") == 0) {
        wchar_t pathW[MAX_PATH];
        // Ensure we handle wide paths properly even if they are long
        if (MultiByteToWideChar(CP_UTF8, 0, argv[2], -1, pathW, MAX_PATH) == 0) {
            printf("0\n");
            return 0;
        }
        unsigned long long size = get_directory_size(pathW);
        printf("%llu\n", size);
        return 0;
    }

    const char *drive_a = argv[1];
    const char *db_path = argv[2];

    wchar_t drive_w[MAX_PATH];
    MultiByteToWideChar(CP_UTF8, 0, drive_a, -1, drive_w, MAX_PATH);

    fast_count(drive_w);
    printf("FINAL_COUNT|%lld\n", global_count);
    fflush(stdout);

    if (sqlite3_open(db_path, &db) != SQLITE_OK) return 1;
    
    // Match Python's concurrency settings
    sqlite3_exec(db, "PRAGMA journal_mode=WAL;", NULL, NULL, NULL);
    sqlite3_exec(db, "PRAGMA synchronous=NORMAL;", NULL, NULL, NULL); 
    sqlite3_busy_timeout(db, 2000); // Wait up to 2s if Python is searching
    
    // Optimized SQLite settings for high-speed writes
    sqlite3_exec(db, "PRAGMA cache_size=-64000;", NULL, NULL, NULL);
    sqlite3_exec(db, "BEGIN TRANSACTION;", NULL, NULL, NULL);
    
    sqlite3_prepare_v2(db, "INSERT OR REPLACE INTO files (type, path, size_raw, size_display) VALUES (?, ?, ?, ?)", -1, &stmt, NULL);
    
    global_count = 0; 
    index_files(drive_w);

    sqlite3_finalize(stmt);
    sqlite3_exec(db, "COMMIT;", NULL, NULL, NULL);

    prune_database(db);

    sqlite3_close(db);
    printf("STATUS|Indexing Complete\n");
    return 0;
}