function setCookie (name, value, expires, domain, path) {
  if (expires) {
    expires = new Date(+new Date() + expires);
  } else {
    expires = new Date(+new Date() + 31536000000);
  }
  var tempcookie = name + '=' + encodeURIComponent(value) +
    ((expires) ? '; expires=' + expires.toGMTString() : '') +
    ((path) ? '; path=' + path : '; path=/') +
    ((domain) ? '; domain=' + domain : '; domain=' + document.domain);

  if (tempcookie.length < 4096) {
    document.cookie = tempcookie;
  }
}
function getCookie (name) {
  var arr, reg = new RegExp("(^| )" + name + "=([^;]*)(;|$)");
  if (arr = document.cookie.match(reg)) {
    return decodeURIComponent(arr[2]);
  } else {
    return null;
  }
}
// 内容页
function setFont (val) {
  $('#content').css('fontFamily', val);
  setCookie('fontFamily', val);
}
function setFontColor (val) {
  $('#content').css('color', val);
  setCookie('fontColor', val);
}
function setSize (val, ele) {
  $('#content').css('fontSize', val);
  setCookie('fontSize', val);
  if (ele) {
    $(ele).addClass('active').siblings().removeClass('active');
  }
}
function setBg (val) {
  $('body').css('backgroundColor', val);
  setCookie('bg', val);
}
function setNight () {
  if ($('#readermode').attr('checked') == true) {
    $('body').css('backgroundColor', '#111111');
    $('.content').css('color', '#555555');
    setCookie('night', 1);
  } else {
    $('body').css('backgroundColor', '');
    $('.content').css('color', '');
    setCookie('night', 0);
  }
}
$('.select-font').change(function () {
  var val = $(this).val()
  setFont(val)
});
$('.select-color').change(function () {
  var val = $(this).val()
  setFontColor(val)
});
$('.select-size').change(function () {
  var val = $(this).val()
  setSize(val)
});
$('.select-bg').change(function () {
  var val = $(this).val()
  setBg(val)
});
// pc set night
$('#readermode').change(function () {
  setNight();
})

var _font = getCookie('fontFamily');
  var _color = getCookie('fontColor');
  var _size = getCookie('fontSize');
  var _bg = getCookie('bg');
  var _night = getCookie('night');

  $('.select-font option').each(function () {
    if ($(this).val() == _font) { $(this).attr('selected', 'selected'); }
  });
  $('.select-color option').each(function () {
    if ($(this).val() == _color) { $(this).attr('selected', 'selected'); }
  });
  $('.select-size option').each(function () {
    if ($(this).val() == _size) { $(this).attr('selected', 'selected'); }
  });
  $('.select-bg option').each(function () {
    if ($(this).val() == _bg) { $(this).attr('selected', 'selected'); }
  });
  setFont(_font);
  setFontColor(_color);
  setSize(_size);
  setBg(_bg);
  if (_night == 1) {
    $('#readermode').click();
    setNight()
  }
  
  