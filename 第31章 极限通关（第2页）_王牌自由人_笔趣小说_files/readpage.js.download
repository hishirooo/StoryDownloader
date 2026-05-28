var ReadSet = {
	bgcolor : ["transparent", "#fafad2", "#e6fae4", "#e0ffff", "#eeeeee", "#e1eced"],
	bgcname : ["默认", "明黄", "护眼", "淡蓝", "淡灰", "淡青"],
	bgcvalue : "transparent",
	
	fontcolor : ["#666666","#666666","#666666","#666666","#333333","#111111"],
	fontcname : ["1", "2", "3","4","5","6"],
	fontcvalue : "#666666",
	
	fontsize : [".75rem", ".875rem", "1rem", "1.125rem",  "1.25rem"],
	fontsname : ["超小", "小", "中", "大", "超大"],
	fontsvalue : "1rem",
    pageid: "wrapper",
    textid: "content",
    SetBgcolor: function(color) {
        document.getElementById(this.pageid).style.backgroundColor = color;
        if (this.bgcvalue != color) this.SetCookies("bgcolor", color);
        this.bgcvalue = color;
    },
	SetFontcolor : function(fcolor){
		document.getElementById(this.textid).style.color = fcolor;
		if(this.fontcvalue != fcolor) this.SetCookies("fontcolor",fcolor);
		this.fontcvalue = fcolor;
	},
    SetFontsize: function(size) {
        document.getElementById(this.textid).style.fontSize = size;
        if (this.fontsvalue != size) this.SetCookies("fontsize", size);
        this.fontsvalue = size;
    },
    Show: function() {
        var output;
        output = '';
        for (i = 0; i < this.bgcolor.length; i++) {
            output += '<a style="background-color: ' + this.bgcolor[i] + '" class="ra" title="' + this.bgcname[i] + '" onclick="ReadSet.SetBgcolor(\'' + this.bgcolor[i] + '\');ReadSet.SetFontcolor(\''+this.fontcolor[i]+'\')" href="javascript:;"></a>';
        }
        output += '<span class="rf">';
        for (i = 0; i < this.fontsize.length; i++) {
            output += '<a class="rt" onclick="ReadSet.SetFontsize(\'' + this.fontsize[i] + '\')" href="javascript:;">' + this.fontsname[i] + '</a>';
        }
        output += '</span>';
        document.getElementById("readSet").innerHTML = output;
    },
    SetCookies: function(cookieName, cookieValue, expirehours) {
        var today = new Date();
        var expire = new Date();
        expire.setTime(today.getTime() + 3600000 * 356 * 24);
        document.cookie = cookieName + '=' + escape(cookieValue) + ';expires=' + expire.toGMTString() + '; path=/';
    },
    ReadCookies: function(cookieName) {
        var theCookie = '' + document.cookie;
        var ind = theCookie.indexOf(cookieName);
        if (ind == -1 || cookieName == '') return '';
        var ind1 = theCookie.indexOf(';', ind);
        if (ind1 == -1) ind1 = theCookie.length;
        return unescape(theCookie.substring(ind + cookieName.length + 1, ind1));
    },
    SaveSet: function() {
        this.SetCookies("bgcolor", this.bgcvalue);
        this.SetCookies("fontcolor", this.fontcvalue);
        this.SetCookies("fontsize", this.fontsvalue);
    },
    LoadSet: function() {
        tmpstr = this.ReadCookies("bgcolor");
        if (tmpstr != "") this.bgcvalue = tmpstr;
        this.SetBgcolor(this.bgcvalue);
        
        tmpstr = this.ReadCookies("fontcolor");
        if (tmpstr != "") this.fontcvalue = tmpstr;
        this.SetFontcolor(this.fontcvalue);
        
        tmpstr = this.ReadCookies("fontsize");
        if (tmpstr != "") this.fontsvalue = tmpstr;
        this.SetFontsize(this.fontsvalue);
    }
}
ReadSet.Show();

function LoadReadSet() {
    ReadSet.LoadSet();
}
if (window.attachEvent) {
    window.attachEvent('onload', LoadReadSet);
} else {
    window.addEventListener('load', LoadReadSet, false);
}

function utf16to8(str) { 
    var out, i, len, c; 
 
    out = ""; 
    len = str.length; 
    for(i = 0; i < len; i++) { 
        c = str.charCodeAt(i); 
        if ((c >= 0x0001) && (c <= 0x007F)) { 
            out += str.charAt(i); 
        } else if (c > 0x07FF) { 
            out += String.fromCharCode(0xE0 | ((c >> 12) & 0x0F)); 
            out += String.fromCharCode(0x80 | ((c >>  6) & 0x3F)); 
            out += String.fromCharCode(0x80 | ((c >>  0) & 0x3F)); 
        } else { 
            out += String.fromCharCode(0xC0 | ((c >>  6) & 0x1F)); 
            out += String.fromCharCode(0x80 | ((c >>  0) & 0x3F)); 
        } 
    } 
    return out; 
} 
function utf8to16(str) { 
    var out, i, len, c; 
    var char2, char3; 
 
    out = ""; 
    len = str.length; 
    i = 0; 
    while(i < len) { 
        c = str.charCodeAt(i++); 
        switch(c >> 4) 
        { 
          case 0: case 1: case 2: case 3: case 4: case 5: case 6: case 7: 
            // 0xxxxxxx 
            out += str.charAt(i-1); 
            break; 
          case 12: case 13: 
            // 110x xxxx   10xx xxxx 
            char2 = str.charCodeAt(i++); 
            out += String.fromCharCode(((c & 0x1F) << 6) | (char2 & 0x3F)); 
            break; 
          case 14: 
            // 1110 xxxx  10xx xxxx  10xx xxxx 
            char2 = str.charCodeAt(i++); 
            char3 = str.charCodeAt(i++); 
            out += String.fromCharCode(((c & 0x0F) << 12) | 
                                           ((char2 & 0x3F) << 6) | 
                                           ((char3 & 0x3F) << 0)); 
            break; 
        } 
    } 
 
    return out; 
} 

var base64EncodeChars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
var base64DecodeChars = new Array(
    -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 
    -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 
    -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 62, -1, -1, -1, 63, 
    52, 53, 54, 55, 56, 57, 58, 59, 60, 61, -1, -1, -1, -1, -1, -1, 
    -1,  0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 
    15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, -1, -1, -1, -1, -1, 
    -1, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 
    41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, -1, -1, -1, -1, -1); 
 
function base64encode(str) { 
    var out, i, len; 
    var c1, c2, c3; 
 
    len = str.length; 
    i = 0; 
    out = ""; 
    while(i < len) { 
        c1 = str.charCodeAt(i++) & 0xff; 
        if(i == len) 
        { 
            out += base64EncodeChars.charAt(c1 >> 2); 
            out += base64EncodeChars.charAt((c1 & 0x3) << 4); 
            out += "=="; 
            break; 
        } 
        c2 = str.charCodeAt(i++); 
        if(i == len) 
        { 
            out += base64EncodeChars.charAt(c1 >> 2); 
            out += base64EncodeChars.charAt(((c1 & 0x3)<< 4) | ((c2 & 0xF0) >> 4)); 
            out += base64EncodeChars.charAt((c2 & 0xF) << 2); 
            out += "="; 
            break; 
        } 
        c3 = str.charCodeAt(i++); 
        out += base64EncodeChars.charAt(c1 >> 2); 
        out += base64EncodeChars.charAt(((c1 & 0x3)<< 4) | ((c2 & 0xF0) >> 4)); 
        out += base64EncodeChars.charAt(((c2 & 0xF) << 2) | ((c3 & 0xC0) >>6)); 
        out += base64EncodeChars.charAt(c3 & 0x3F); 
    } 
    return out; 
} 
 
function base64decode(str) { 
    var c1, c2, c3, c4; 
    var i, len, out; 
 
    len = str.length; 
    i = 0; 
    out = ""; 
    while(i < len) { 
        /* c1 */ 
        do { 
            c1 = base64DecodeChars[str.charCodeAt(i++) & 0xff]; 
        } while(i < len && c1 == -1); 
        if(c1 == -1) 
            break; 
 
        /* c2 */ 
        do { 
            c2 = base64DecodeChars[str.charCodeAt(i++) & 0xff]; 
        } while(i < len && c2 == -1); 
        if(c2 == -1) 
            break; 
 
        out += String.fromCharCode((c1 << 2) | ((c2 & 0x30) >> 4)); 
 
        /* c3 */ 
        do { 
            c3 = str.charCodeAt(i++) & 0xff; 
            if(c3 == 61) 
                return out; 
            c3 = base64DecodeChars[c3]; 
        } while(i < len && c3 == -1); 
        if(c3 == -1) 
            break; 
 
        out += String.fromCharCode(((c2 & 0XF) << 4) | ((c3 & 0x3C) >> 2)); 
 
        /* c4 */ 
        do { 
            c4 = str.charCodeAt(i++) & 0xff; 
            if(c4 == 61) 
                return out; 
            c4 = base64DecodeChars[c4]; 
        } while(i < len && c4 == -1); 
        if(c4 == -1) 
            break; 
        out += String.fromCharCode(((c3 & 0x03) << 6) | c4); 
    } 
    return out; 
} 
 
function str_decode(str){ 
        return utf8to16(base64decode(str)); 
} 
function str_encode(str){ 
    return base64encode(utf16to8(str)); 
}