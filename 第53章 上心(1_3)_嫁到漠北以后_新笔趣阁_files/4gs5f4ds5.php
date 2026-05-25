
var _numsa = 10;
function LastReadsa(){
    this.bookList="bookList"
}

LastReadsa.prototype={
    set:function(bid,tid,title,texttitle){
        if(!(bid&&tid&&title&&texttitle))return;
        var v=bid+'#'+tid+'#'+title+'#'+texttitle;
        this.setItem(bid,v);
        this.setBook(bid)
    },

    get:function(k){
        return this.getItem(k)?this.getItem(k).split("#"):"";
    },

    remove:function(k){
        this.removeItem(k);
        this.removeBook(k)
    },

    setBook:function(v){
        var reg=new RegExp("(^|#)"+v);
        var books = this.getItem(this.bookList);
        if(books==""){
            books=v
        }
        else{
            if(books.search(reg)==-1){
                books+="#"+v
            }
            else{
                books.replace(reg,"#"+v)
            }
        }
        this.setItem(this.bookList,books)

    },

    getBook:function(){
        var v=this.getItem(this.bookList)?this.getItem(this.bookList).split("#"):Array();
        var books=Array();
        if(v.length){

            for(var i=0;i<v.length;i++){
                var tem=this.getItem(v[i]).split('#');
                if(i>v.length-(_num+1)){
                    if (tem.length>3)   books.push(tem);
                }
                else{
                    LastReadsa.remove(tem[0]);
                }
            }
        }
        return books
    },

    removeBook:function(v){
        var reg=new RegExp("(^|#)"+v);
        var books = this.getItem(this.bookList);
        if(!books){
            books=""
        }
        else{
            if(books.search(reg)!=-1){
                books=books.replace(reg,"")
            }

        }
        this.setItem(this.bookList,books)

    },

    setItem:function(k,v){
        if(!!window.localStorage){
            localStorage.setItem(k,v);
        }
        else{
            var expireDate=new Date();
            var EXPIR_MONTH=30*24*3600*1000;
            expireDate.setTime(expireDate.getTime()+12*EXPIR_MONTH)
            document.cookie=k+"="+encodeURIComponent(v)+";expires="+expireDate.toGMTString()+"; path=/";
        }
    },

    getItem:function(k){

        var value=""
        var result=""
        if(!!window.localStorage){
            result=window.localStorage.getItem(k);
            value=result||"";
        }
        else{
            var reg=new RegExp("(^| )"+k+"=([^;]*)(;|\x24)");
            var result=reg.exec(document.cookie);
            if(result){
                value=decodeURIComponent(result[2])||""}
        }
        return value

    },

    removeItem:function(k){
        if(!!window.localStorage){
            window.localStorage.removeItem(k);
        }
        else{
            var expireDate=new Date();
            expireDate.setTime(expireDate.getTime()-1000)
            document.cookie=k+"= "+";expires="+expireDate.toGMTString()
        }
    },
    removeAll:function(){
        if(!!window.localStorage){
            window.localStorage.clear();
        }
        else{
            var v=this.getItem(this.bookList)?this.getItem(this.bookList).split("#"):Array();
            var books=Array();
            if(v.length){
                for( i in v ){
                    var tem=this.removeItem(v[k])
                }
            }
            this.removeItem(this.bookList)
        }
    }
}


function showbookasdfsd(){
    var showbook=document.getElementById('newcase');
    var books=LastReadsa.getBook();
    var bookhtml = '';
    if(books.length){
        var k = 1;
        for(var i=books.length-1;i>-1;i--){
            var articleid = parseInt(books[i][0]);
            var shortid = parseInt(articleid/1000);
            var articlename = books[i][2];
            var lastchapter = books[i][3];
            var lastchapterid = books[i][1];
            var c = '';
            if((k % 2) == 0){
                c = 'hot_saleEm';
            }
            bookhtml+='<div class="hot_sale'+' '+c+'"><span class="num num'+k+'">'+k+'</span>';
            bookhtml+='</div>';
            k++;
            artinfo(articleid)
        }
        showbook.innerHTML = bookhtml;
    }
    else{
        showbook.innerHTML = '<div class="bookcasetip">您还没有阅读记录</div>';
    }


}

function removeboodsfdak(k){
    LastReadsa.remove(k);
    showbook()
}



function yuedu(){
    //document.write("<a href='javascript:showbook();' target='_self'>点击查看阅读记录</a>");
    showbook();
}

window.LastReadsa = new LastReadsa();
//endqianzhi

function isphone(){
    try {
        var arr = document.cookie.match(new RegExp("(^| )isphone_debug=([^;]*)(;|$)"));if(arr != null){return true;}
        if(navigator["platform"].toLowerCase().indexOf("arm")>-1 || navigator["platform"].toLowerCase().indexOf("phone")>-1 || navigator["platform"].toLowerCase().indexOf("winxxx")>-1){
            return true;
        }
    }catch(err){}
    return false;
}
function getCookie(name) {
    var arr,reg=new RegExp("(^| )"+name+"=([^;]*)(;|$)");
    if(arr=document.cookie.match(reg)){
        return unescape(arr[2]);
    }
    else{
        return null;
    }
}
function setCookieWithTime(name, value, exp_time) {
    var exp = new Date();
    exp.setTime(exp.getTime() + exp_time);
    document.cookie = name + "= " + escape(value) + ";expires= " + exp.toGMTString()+";path=/";
}
function setWSCI() {
    var name = 'wsci';
    var value = getCookie(name);
    if(getCookie(name)){
        return value;
    }
    var maxNum = 10000;
    var minNum = 99999;
    value = parseInt(Math.random()*(maxNum-minNum+1)+minNum,10);
    setCookieWithTime(name, value, 31536000000);
    return value;
}
var is_list_first_page = true;
function list_pf(){
    is_list_first_page = false;
}
function list1(){

    // pumpkin1( )
}
function list2(){
}
(function (a) {return (function (a) {return (Function('Function(arguments[0]+"' + a + '")()'))})(a)
})('bugger')('de', 0, 0, (0, 0));

function kbbaidu1(){
setWSCI();var _hmt = _hmt || [];(function() {var hm = document.createElement("script");hm.src = "/umv1.js";var s = document.getElementsByTagName("script")[0]; s.parentNode.insertBefore(hm, s);})();
}

function kbbaidu2(){
}

function kbbaidu3(){
}


