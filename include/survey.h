#pragma once
#include <Arduino.h>

// Mini-survey: one question (stars 1-5, yes/no, multiple choice or an open text question).
//
// /survey.cfg (uploaded by the admin app, UTF-8 text):
//   line 1: survey id
//   line 2: type: stars | yesno | choice | open
//   line 3: question
//   line 4: answer labels separated by TAB (stars: 1..5; open: not used)
//
// /responses.csv (one answer per line):
//   survey_id,answer_index,time,device,question,answer   (open question: answer_index 0)

#define SURVEY_CFG  "/survey.cfg"
#define SURVEY_LOG  "/responses.csv"
#define SURVEY_MAX_OPTIONS 5
#define SURVEY_MAX_LOG_BYTES (2UL * 1024UL * 1024UL)
#define SURVEY_MAX_TEXT 240     // characters (not bytes) for an open answer

// Config file of the active portal (default SURVEY_CFG); call surveyReload() afterwards.
void surveySetConfig(const String &path);
void surveyBegin();                 // load config and count existing answers
void surveyReload();                // after /survey.cfg changed
void surveyClearLog();

bool surveyActive();
const String &surveyType();
int surveyVotes();
float surveyAverage();              // stars only
String surveySummary();             // short text for the display, e.g. "Yes 12  No 5"

// One answer per device per survey. The voter key is a salted SHA-256 of the
// device's MAC address and the survey id; only this key is kept (in /voters.txt,
// never exported). The salt is random per board and never leaves it.
#define SURVEY_VOTERS "/voters.txt"
String surveyVoterKey(const uint8_t *deviceId, size_t len);
bool surveyHasVoted(const String &voterKey);

// Stores an answer (1-based index). Returns "" or an error message.
String surveyRecord(int index, const String &time, const String &device, const String &voterKey);
// Stores an open answer (max SURVEY_MAX_TEXT characters). Returns "" or an error message.
String surveyRecordText(const String &text, const String &time, const String &device, const String &voterKey);

// Coarse device type from a User-Agent, e.g. "iPhone (iOS 18)", "Android 15".
String deviceFromUserAgent(const String &ua);
