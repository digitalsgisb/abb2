var PDF_FOLDER_ID = "1nEckc0ZmX4j1UKjWZT_2pv1e7grfxcWr"; 

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    var payload = JSON.parse(e.postData.contents);
    var action = payload.action;
    
    if (action === "APPEND_ROW") {
      var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(payload.tab_name);
      sheet.appendRow(payload.row_data);
      return ContentService.createTextOutput(JSON.stringify({"status": "success"})).setMimeType(ContentService.MimeType.JSON);
    } 
    
    else if (action === "GENERATE_PDF") {
      var shiftId = payload.shift_id;
      var pdfUrl = generateShiftPDF(shiftId);
      return ContentService.createTextOutput(JSON.stringify({
        "status": "success", 
        "message": "PDF Generated!",
        "url": pdfUrl
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // ==============================================================
    // NEW: Action to scan Google Drive and get a list of available shifts
    // ==============================================================
    else if (action === "GET_SHIFT_LIST") {
      var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
      var files = folder.searchFiles("mimeType = 'application/pdf'");
      var shiftSet = {}; 
      
      // Loop through all PDFs in the folder
      while (files.hasNext()) {
        var file = files.next();
        var name = file.getName(); // Example: "PRS_20260313-Day-Line1_101405.pdf"
        
        // Chop up the name based on the underscores
        var parts = name.split("_");
        if (parts.length >= 2) {
          var shiftId = parts[1]; // This grabs just "20260313-Day-Line1"
          shiftSet[shiftId] = true; // Saves it to our list (automatically prevents duplicates!)
        }
      }
      
      // Convert our list to an array and sort it so the newest shifts are at the top
      var shiftList = Object.keys(shiftSet).sort().reverse(); 
      
      return ContentService.createTextOutput(JSON.stringify({
        "status": "success", 
        "shift_list": shiftList
      })).setMimeType(ContentService.MimeType.JSON);
    }
    
    // ==============================================================
    // NEW: Action to search Drive and return the LATEST iframe link
    // ==============================================================
    else if (action === "GET_PDF_URL") {
      var shiftId = payload.shift_id;
      var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
      var files = folder.searchFiles("title contains '" + shiftId + "' and mimeType = 'application/pdf'");
      
      var latestFile = null;
      var latestDate = 0;
      
      // Loop through ALL files found for this shift
      while (files.hasNext()) {
        var currentFile = files.next();
        var fileDate = currentFile.getDateCreated().getTime(); // Get the exact millisecond it was created
        
        // If this file is newer than the last one we checked, save it!
        if (fileDate > latestDate) {
          latestDate = fileDate;
          latestFile = currentFile;
        }
      }
      
      // Now, if we actually found a newest file, generate the link!
      if (latestFile) {
        latestFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
        var iframeUrl = "https://drive.google.com/file/d/" + latestFile.getId() + "/preview";
        
        return ContentService.createTextOutput(JSON.stringify({
          "status": "success", 
          "url": iframeUrl
        })).setMimeType(ContentService.MimeType.JSON);
      } else {
        return ContentService.createTextOutput(JSON.stringify({
          "status": "error", 
          "message": "No PDF found for this shift yet."
        })).setMimeType(ContentService.MimeType.JSON);
      }
    }
    
  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({"status": "error", "message": error.message})).setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

function generateShiftPDF(shiftId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  
  var templateSheet = ss.getSheetByName("PRS_Template");
  if (!templateSheet) {
    throw new Error("Could not find 'PRS_Template' tab.");
  }
  var shiftDataSheet = ss.getSheetByName("Shift_Data");
  
  var uniqueTempName = "TEMP_" + shiftId + "_" + new Date().getTime();
  var tempSheet = templateSheet.copyTo(ss);
  tempSheet.setName(uniqueTempName);
  
  try {
    var data = shiftDataSheet.getDataRange().getValues();
    var targetShift = null;
    
    for (var i = 1; i < data.length; i++) {
      if (data[i][0] === shiftId) {
        targetShift = data[i];
        break; 
      }
    }
    
    if (targetShift) {
      tempSheet.getRange("C3").setValue(targetShift[2]); // Line
      tempSheet.getRange("G3").setValue(targetShift[3]); // Shift (A/B/N)
      tempSheet.getRange("C4").setValue(targetShift[1]); // Date
      tempSheet.getRange("H4").setValue(targetShift[5]); // Working Time
      tempSheet.getRange("N3").setValue(targetShift[6]); // Supervisor
      tempSheet.getRange("N4").setValue(targetShift[7]); // Leader
      tempSheet.getRange("V3").setValue(targetShift[8]); // Forming Operator
      tempSheet.getRange("V4").setValue(targetShift[9]); // Waterjet Operator
      tempSheet.getRange("AC3").setValue(targetShift[10]); // Assembly Operator
      tempSheet.getRange("AC4").setValue(targetShift[11]); // Quality Inspector
      
      // ==============================================================
      // SMART SHIFT DETECTION
      // ==============================================================
      var shiftType = targetShift[3].toString().toLowerCase(); 
      
      var rowMapDay = {
        "8": 10, "9": 12, "10": 14, "11": 16, "12": 18, 
        "13": 20, "1": 20,   
        "14": 22, "2": 22,   
        "15": 24, "3": 24,   
        "16": 26, "4": 26,   
        "17": 28, "5": 28,   
        "18": 30, "6": 30,   
        "19": 32, "7": 32    
      };
      
      var rowMapNight = {
        "20": 10, "8": 10,    
        "21": 12, "9": 12,   
        "22": 14, "10": 14,  
        "23": 16, "11": 16,  
        "0": 18,  "12": 18,  
        "1": 20,  "13": 20,  
        "2": 22,  "14": 22,  
        "3": 24,  "15": 24,  
        "4": 26,  "16": 26,  
        "5": 28,  "17": 28,  
        "6": 30,  "18": 30,  
        "7": 32,  "19": 32   
      };
      
      var rowMap = shiftType.includes("night") ? rowMapNight : rowMapDay;

      // ==============================================================
      // PRE-FILL REJECT & DOWNTIME CELLS WITH "-"
      // ==============================================================
      var uniqueRows = [];
      for (var key in rowMap) {
        if (uniqueRows.indexOf(rowMap[key]) === -1) {
          uniqueRows.push(rowMap[key]);
        }
      }
      
      for (var u = 0; u < uniqueRows.length; u++) {
        var r = uniqueRows[u];
        tempSheet.getRange("N" + r + ":U" + r).setValue("-");   // Reject columns
        tempSheet.getRange("AA" + r + ":AF" + r).setValue("-"); // Downtime columns
        tempSheet.getRange("AA" + (r + 1)).setValue("-");       // Remarks box
      }

      // ==============================================================
      // 4. MAP HOURLY DATA & CALCULATE TOTALS
      // ==============================================================
      var hourlySheet = ss.getSheetByName("Hourly_Data");
      var modelTotals = {};

      if (hourlySheet) {
        var hourlyData = hourlySheet.getDataRange().getValues();
        for (var h = 1; h < hourlyData.length; h++) {
          if (hourlyData[h][0] === shiftId) {
            var startHourStr = String(hourlyData[h][3]).replace(":", ".").split(".")[0];
            var rowNum = rowMap[startHourStr];
            
            if (rowNum) {
              var modelName = hourlyData[h][2];
              var actualQty = parseInt(hourlyData[h][5], 10) || 0; 
              
              tempSheet.getRange("B" + rowNum).setValue(modelName); 
              tempSheet.getRange("E" + rowNum).setValue(hourlyData[h][4]); 
              tempSheet.getRange("F" + rowNum).setValue(actualQty); 
              tempSheet.getRange("G" + rowNum).setValue(hourlyData[h][6]); 
              
              var rawSeconds = hourlyData[h][7];
              if (rawSeconds !== "" && rawSeconds > 0) {
                var totalSecs = parseInt(rawSeconds, 10); 
                var mins = Math.floor(totalSecs / 60);    
                var secs = totalSecs % 60;                
                var formattedSecs = secs < 10 ? "0" + secs : secs;
                tempSheet.getRange("D" + (rowNum + 1)).setValue(mins + "." + formattedSecs);
              }

              if (modelName && modelName.toString().trim() !== "") {
                if (!modelTotals[modelName]) {
                  modelTotals[modelName] = 0; 
                }
                modelTotals[modelName] += actualQty; 
              }
            }
          }
        }
      }

      // ==============================================================
      // 5. MAP REJECT DATA
      // ==============================================================
      var rejectSheet = ss.getSheetByName("Reject_Data");
      if (rejectSheet) {
        var rejectData = rejectSheet.getDataRange().getValues();
        for (var r_idx = 1; r_idx < rejectData.length; r_idx++) {
          if (rejectData[r_idx][0] === shiftId) {
            var startHourR = String(rejectData[r_idx][1]).replace(":", ".").split(".")[0];
            var rowNumR = rowMap[startHourR];
            if (rowNumR) {
              tempSheet.getRange("N" + rowNumR).setValue(rejectData[r_idx][2] !== "" ? rejectData[r_idx][2] : "-");
              tempSheet.getRange("O" + rowNumR).setValue(rejectData[r_idx][3] !== "" ? rejectData[r_idx][3] : "-");
              tempSheet.getRange("P" + rowNumR).setValue(rejectData[r_idx][4] !== "" ? rejectData[r_idx][4] : "-");
              tempSheet.getRange("Q" + rowNumR).setValue(rejectData[r_idx][5] !== "" ? rejectData[r_idx][5] : "-");
              tempSheet.getRange("R" + rowNumR).setValue(rejectData[r_idx][6] !== "" ? rejectData[r_idx][6] : "-");
              tempSheet.getRange("S" + rowNumR).setValue(rejectData[r_idx][7] !== "" ? rejectData[r_idx][7] : "-");
              tempSheet.getRange("T" + rowNumR).setValue(rejectData[r_idx][8] !== "" ? rejectData[r_idx][8] : "-");
              tempSheet.getRange("U" + rowNumR).setValue(rejectData[r_idx][9] !== "" ? rejectData[r_idx][9] : "-");
            }
          }
        }
      }

      // ==============================================================
      // 6. MAP DOWNTIME DATA & ROW-BY-ROW REMARKS
      // ==============================================================
      var downtimeSheet = ss.getSheetByName("Downtime_Data");
      if (downtimeSheet) {
        var downtimeData = downtimeSheet.getDataRange().getValues();
        for (var d = 1; d < downtimeData.length; d++) {
          if (downtimeData[d][0] === shiftId) {
            var startHourD = String(downtimeData[d][1]).replace(":", ".").split(".")[0]; 
            var rowNumD = rowMap[startHourD];
            
            if (rowNumD) {
              var category = downtimeData[d][2] ? downtimeData[d][2].toString().toLowerCase() : "";
              var code = downtimeData[d][3] !== "" ? downtimeData[d][3] : "-";
              var duration = downtimeData[d][4] !== "" ? downtimeData[d][4] : "-";

              if (category.includes("schedule")) {
                tempSheet.getRange("AA" + rowNumD).setValue(code);
                tempSheet.getRange("AB" + rowNumD).setValue(duration);
              } else if (category.includes("quality") || category.includes("production")) {
                tempSheet.getRange("AC" + rowNumD).setValue(code);
                tempSheet.getRange("AD" + rowNumD).setValue(duration);
              } else if (category.includes("machine") || category.includes("mechanical") || category.includes("stop")) {
                tempSheet.getRange("AE" + rowNumD).setValue(code);
                tempSheet.getRange("AF" + rowNumD).setValue(duration);
              }

              var remarkText = downtimeData[d][6]; 
              if (remarkText && remarkText.toString().trim() !== "") {
                var remarkRow = rowNumD + 1; 
                var remarkCell = tempSheet.getRange("AA" + remarkRow); 
                var currentText = remarkCell.getValue();
                
                if (currentText === "-") {
                  remarkCell.setValue(remarkText);
                } else if (currentText !== "") {
                  remarkCell.setValue(currentText + "\n" + remarkText);
                } else {
                  remarkCell.setValue(remarkText);
                }
              }
            }
          }
        }
      }

      // ==============================================================
      // 7. MAP SUMMARY TABLE (Calculated Totals)
      // ==============================================================
      var summaryCols = ["AD", "AE", "AF", "AG"]; 
      var uniqueModels = Object.keys(modelTotals); 
      
      for (var m = 0; m < uniqueModels.length && m < 4; m++) {
        var col = summaryCols[m];
        var modName = uniqueModels[m];
        var modTotal = modelTotals[modName];
        
        tempSheet.getRange(col + "54").setValue(modName);   
        tempSheet.getRange(col + "56").setValue(modTotal);  
      }

      // ==============================================================
      // 8. MAP PARAMETER DATA (TABLES 1, 2, & 3)
      // ==============================================================
      var paramSheet = ss.getSheetByName("Parameter_Data");
      if (paramSheet) {
        var paramData = paramSheet.getDataRange().getValues();
        
        // Arrays to track which starting rows to paste into for each table
        var t1Rows = [41, 43, 45, 47]; // Table 1: Parameter Condition
        var t2Rows = [55, 57, 59];     // Table 2: Temperature Condition
        var t3Rows = [41, 45];     // Table 3: Glue Consumption

        var t1Count = 0;
        var t2Count = 0;
        var t3Count = 0;

        // Keep track of models we've already added so we don't print duplicates
        var processedModelsT1 = [];
        var processedModelsT2 = [];
        var processedModelsT3 = [];

        for (var p = 1; p < paramData.length; p++) {
          if (paramData[p][0] === shiftId) {
            var model = paramData[p][1];
            
            // --- TABLE 1: Parameter Condition ---
            if (t1Count < t1Rows.length && processedModelsT1.indexOf(model) === -1) {
              var r1 = t1Rows[t1Count];
              tempSheet.getRange("V" + r1).setValue(model);
              tempSheet.getRange("W" + r1).setValue(paramData[p][2]); // Heating
              tempSheet.getRange("X" + r1).setValue(paramData[p][3]); // Cooling
              tempSheet.getRange("Y" + r1).setValue(paramData[p][4]); // Shuttle
              tempSheet.getRange("Z" + r1).setValue(paramData[p][5]); // Waterjet
              processedModelsT1.push(model);
              t1Count++;
            }

            // --- TABLE 2: Temperature Condition ---
            if (t2Count < t2Rows.length && processedModelsT2.indexOf(model) === -1) {
              var r2 = t2Rows[t2Count];
              tempSheet.getRange("V" + r2).setValue(model);
              tempSheet.getRange("W" + r2).setValue(paramData[p][6]); // Temp RH
              tempSheet.getRange("X" + r2).setValue(paramData[p][7]); // Temp CTR
              tempSheet.getRange("Y" + r2).setValue(paramData[p][8]); // Temp LH
              processedModelsT2.push(model);
              t2Count++;
            }

            // --- TABLE 3: Glue Consumption ---
            var glueStd = paramData[p][9];
            var glueAct = paramData[p][10];
            
            // NEW: Check if there is an actual glue value (not empty and not a dash)
            var hasGlue = (glueStd !== "" && glueStd !== "-") || (glueAct !== "" && glueAct !== "-");

            // Only print if it actually has glue data!
            if (hasGlue) {
              if (t3Count < t3Rows.length && processedModelsT3.indexOf(model) === -1) {
                var r3 = t3Rows[t3Count];
                tempSheet.getRange("AD" + r3).setValue(model);
                tempSheet.getRange("AE" + r3).setValue(glueStd); // Glue STD
                tempSheet.getRange("AF" + r3).setValue(glueAct); // Glue ACT
                processedModelsT3.push(model);
                t3Count++;
              }
            }
          }
        }
      }
    }
    
    SpreadsheetApp.flush();
    
    var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
    var url = "https://docs.google.com/spreadsheets/d/" + ss.getId() + "/export?exportFormat=pdf&format=pdf&size=A3&portrait=false&fitw=true&top_margin=0.10&bottom_margin=0.10&left_margin=0.10&right_margin=0.10&gid=" + tempSheet.getSheetId();

    var token = ScriptApp.getOAuthToken();
    var response = UrlFetchApp.fetch(url, { headers: { 'Authorization': 'Bearer ' + token } });
    
    var pdfName = "PRS_" + shiftId + "_" + Utilities.formatDate(new Date(), "GMT+8", "HHmmss") + ".pdf";
    var blob = response.getBlob().setName(pdfName);
    var newFile = folder.createFile(blob);
    
    // ==============================================================
    // NEW: Update file permissions and return the iframe preview link!
    // ==============================================================
    newFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    var iframeUrl = "https://drive.google.com/file/d/" + newFile.getId() + "/preview";
    
    return iframeUrl;
    
  } finally {
    ss.deleteSheet(tempSheet);
  }
}

function testRun() {
  var testShiftId = "20260313-Day-Line1"; 
  Logger.log("Starting test for Shift ID: " + testShiftId);
  var resultUrl = generateShiftPDF(testShiftId);
  Logger.log("Test Complete! PDF URL: " + resultUrl);
}